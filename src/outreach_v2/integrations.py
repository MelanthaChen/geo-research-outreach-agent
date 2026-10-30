"""Real authenticated transports. Missing configuration is an explicit block."""
from __future__ import annotations
import os,json,base64
from collections import Counter,defaultdict
from dataclasses import dataclass
from datetime import datetime,timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo
import httpx
from .consent import REQUIRED_HEADERS
from outreach_agent.google_forms_integration import FORM_ID,RESPONSE_SPREADSHEET_ID

class ConfigurationBlocked(RuntimeError):pass
class OutcomeUncertain(RuntimeError):pass

SHEETS_SCOPE='https://www.googleapis.com/auth/spreadsheets.readonly'
FORMS_BODY_SCOPE='https://www.googleapis.com/auth/forms.body.readonly'
FORMS_RESPONSES_SCOPE='https://www.googleapis.com/auth/forms.responses.readonly'
# The six responses present before the owner-controlled integration test.
# Keep them as historical source data; they are not new operational consent.
HISTORICAL_RESPONSE_CUTOFF=datetime.fromisoformat('2026-10-27T22:15:44.227143+00:00')
HEADER_ALIASES={'Your Role / Position':'Job Title of the Authorized Company Representative'}

def canonical_header(header):
    value=str(header or '').strip()
    return HEADER_ALIASES.get(value,value)

def _utc_datetime(value):
    if not value:return None
    try:parsed=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    except ValueError:return None
    if parsed.tzinfo is None:parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _sheet_timestamp_key(value,time_zone):
    parsed=_utc_datetime(value)
    if parsed is None:
        for pattern in ('%m/%d/%Y %H:%M:%S','%m/%d/%Y %H:%M'):
            try:
                parsed=datetime.strptime(str(value).strip(),pattern).replace(tzinfo=ZoneInfo(time_zone)).astimezone(timezone.utc)
                break
            except ValueError:continue
    return parsed.replace(microsecond=0) if parsed else None

def _response_timestamp_key(response):
    parsed=_utc_datetime(response.get('lastSubmittedTime') or response.get('createTime'))
    return parsed.replace(microsecond=0) if parsed else None

def _sheet_row(headers,values):
    """Merge aliases and duplicate headers without allowing blanks to win.

    Conflicting duplicate values blank the canonical field so consent and
    scope checks fail closed for that row.
    """
    grouped=defaultdict(list)
    for header,value in zip(headers,values):
        canonical=canonical_header(header);text=str(value or '').strip()
        if text:grouped[canonical].append(text)
        else:grouped.setdefault(canonical,[])
    return {header:(items[0] if len(set(items))==1 and items else '') for header,items in grouped.items()}

@dataclass(frozen=True)
class VerifiedFormResponse:
    answers:dict
    identity:dict

class SheetRead(list):
    def __init__(self,rows,identity_status='UNAVAILABLE',identity_error=None):
        super().__init__(rows);self.identity_status=identity_status;self.identity_error=identity_error

def _answer_value(answer):
    for group in ('textAnswers','choiceAnswers'):
        values=(answer.get(group) or {}).get('answers') or []
        if values:return ', '.join(str(v.get('value','')) for v in values if v.get('value') is not None)
    return ''

def verified_form_rows(form,responses,sheet_headers):
    """Convert only API-authenticated, VERIFIED Forms responses into consent rows."""
    if (form.get('settings') or {}).get('emailCollectionType')!='VERIFIED':
        raise ConfigurationBlocked('Google Form email collection must be VERIFIED')
    from .consent import REQUIRED_HEADERS
    titles={}
    for item in form.get('items') or []:
        question=((item.get('questionItem') or {}).get('question') or {})
        qid=question.get('questionId');title=str(item.get('title') or '').strip()
        if qid and title:titles[qid]=canonical_header(title)
    required_questions=REQUIRED_HEADERS-{'Timestamp'}
    if not required_questions.issubset(set(titles.values())):
        raise ConfigurationBlocked('Google Form questions do not match the live response-sheet consent contract')
    if not REQUIRED_HEADERS.issubset({canonical_header(h) for h in sheet_headers}):
        raise ConfigurationBlocked('Live Sheet headers do not match the independent consent contract')
    output=[]
    for response in responses:
        submitted=_utc_datetime(response.get('lastSubmittedTime') or response.get('createTime'))
        if not submitted or submitted<=HISTORICAL_RESPONSE_CUTOFF:continue
        respondent=str(response.get('respondentEmail') or '').strip().lower()
        if not respondent:continue
        row={}
        for qid,answer in (response.get('answers') or {}).items():
            title=titles.get(qid)
            if not title:continue
            value=_answer_value(answer)
            if title not in row or not row[title]:row[title]=value
            elif value and row[title]!=value:row[title]=''
        row['Timestamp']=str(response.get('lastSubmittedTime') or response.get('createTime') or '')
        if not row.get('Invitation Reference'):continue
        output.append(VerifiedFormResponse(row,{'provider':'GOOGLE_FORMS_VERIFIED','email':respondent,
            'response_id':str(response.get('responseId') or ''),'email_collection_type':'VERIFIED'}))
    return output

class Sheets:
    def read(self):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        encoded=os.environ.get('GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64')
        path=os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        scopes=[SHEETS_SCOPE,FORMS_BODY_SCOPE,FORMS_RESPONSES_SCOPE]
        if encoded:
            credentials=service_account.Credentials.from_service_account_info(json.loads(base64.b64decode(encoded)),scopes=scopes)
        elif path:
            credentials=service_account.Credentials.from_service_account_file(path,scopes=scopes)
        else:raise ConfigurationBlocked('Authenticated Google Sheets service account is not configured')
        session=AuthorizedSession(credentials)
        meta=session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{RESPONSE_SPREADSHEET_ID}',params={'fields':'properties.timeZone'},timeout=30)
        if meta.status_code!=200:raise ConfigurationBlocked(f'Google Sheets metadata returned HTTP {meta.status_code}')
        sheet_timezone=str((meta.json().get('properties') or {}).get('timeZone') or 'UTC')
        range_name=os.environ.get('GEO_RESEARCH_SHEETS_RANGE','Form Responses 1!A:Z')
        response=session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{RESPONSE_SPREADSHEET_ID}/values/{quote(range_name,safe="")}',timeout=30)
        if response.status_code!=200:raise ConfigurationBlocked(f'Google Sheets returned HTTP {response.status_code}')
        rows=response.json().get('values') or []
        if not rows:raise ConfigurationBlocked('Live response Sheet has no header row')
        canonical_headers=[canonical_header(h) for h in rows[0]]
        if not REQUIRED_HEADERS.issubset(set(canonical_headers)):raise ConfigurationBlocked('Live Sheet headers do not match the independent consent contract')
        all_sheet_rows=[_sheet_row(rows[0],row) for row in rows[1:]]
        # The Sheet's business-email question is self-reported. Only Forms API
        # respondentEmail plus the VERIFIED collection setting can establish
        # independent control of the account that submitted a response.
        try:
            form_response=session.get(f'https://forms.googleapis.com/v1/forms/{FORM_ID}',params={'fields':'formId,settings.emailCollectionType,items.title,items.questionItem.question.questionId'},timeout=30)
            if form_response.status_code!=200:raise ConfigurationBlocked(f'Google Forms API unavailable (HTTP {form_response.status_code})')
            form=form_response.json()
            verified=(form.get('settings') or {}).get('emailCollectionType')=='VERIFIED'
            responses=[];page_token=None
            while True:
                fields='responses(responseId,createTime,lastSubmittedTime,respondentEmail,answers),nextPageToken' if verified else 'responses(responseId,createTime,lastSubmittedTime),nextPageToken'
                params={'pageSize':500,'fields':fields}
                if page_token:params['pageToken']=page_token
                result=session.get(f'https://forms.googleapis.com/v1/forms/{FORM_ID}/responses',params=params,timeout=30)
                if result.status_code!=200:raise ConfigurationBlocked(f'Google Forms response access unavailable (HTTP {result.status_code})')
                payload=result.json();responses.extend(payload.get('responses') or [])
                page_token=payload.get('nextPageToken')
                if not page_token:break
            if verified:return SheetRead(verified_form_rows(form,responses,rows[0]),'VERIFIED')
            api_counts=Counter(key for item in responses if (key:=_response_timestamp_key(item)) and key>HISTORICAL_RESPONSE_CUTOFF)
            sheet_keys=[_sheet_timestamp_key(item.get('Timestamp'),sheet_timezone) for item in all_sheet_rows]
            sheet_counts=Counter(key for key in sheet_keys if key)
            fresh_rows=[item for item,key in zip(all_sheet_rows,sheet_keys)
                        if key and key>HISTORICAL_RESPONSE_CUTOFF and api_counts[key]==1 and sheet_counts[key]==1]
            return SheetRead(fresh_rows,'UNAVAILABLE','Google Form email collection is not VERIFIED')
        except ConfigurationBlocked as exc:
            # Continue processing already-authorized owner fixtures and record
            # ordinary responses as blocked identity, while surfacing the missing
            # verification capability in worker health.
            fresh_rows=[item for item in all_sheet_rows
                        if (key:=_sheet_timestamp_key(item.get('Timestamp'),sheet_timezone)) and key>HISTORICAL_RESPONSE_CUTOFF]
            return SheetRead(fresh_rows,'UNAVAILABLE',str(exc))

class Engine:
    """Requires the durable job API; never falls back to historical fixture runs."""
    def __init__(self):
        url=os.environ.get('GEO_ENGINE_API_URL','').rstrip('/');token=os.environ.get('GEO_ENGINE_HANDOFF_TOKEN')
        if not url.startswith('https://') or not token:raise ConfigurationBlocked('GEO Engine HTTPS URL and service token are required')
        self.client=httpx.Client(base_url=url,headers={'Authorization':'Bearer '+token},timeout=40)
    def reconcile(self,key):
        r=self.client.get('/api/v1/integration/outreach-jobs/'+quote(key,safe=''))
        if r.status_code==404:
            # Separate capability check prevents interpreting an absent route as an absent job.
            caps=self.client.get('/api/v1/integration/outreach-capabilities')
            if caps.status_code in {401,403}:raise ConfigurationBlocked(f'GEO Engine capabilities GET authorization denied: HTTP {caps.status_code}')
            if caps.status_code!=200:raise ConfigurationBlocked(f'GEO Engine capabilities GET failed: HTTP {caps.status_code}')
            if not caps.json().get('durable_jobs'):raise ConfigurationBlocked('Deployed GEO Engine does not provide durable outreach jobs')
            return None
        if r.status_code in {401,403}:raise ConfigurationBlocked(f'GEO Engine job reconciliation GET authorization denied: HTTP {r.status_code}')
        if r.status_code>=400:raise ConfigurationBlocked(f'GEO Engine job reconciliation GET failed: HTTP {r.status_code}')
        return r.json()
    def advance(self,key):
        try:r=self.client.post('/api/v1/integration/outreach-advance/'+quote(key,safe=''))
        except httpx.TransportError as e:raise OutcomeUncertain('Engine execution response uncertain; reconcile the durable job') from e
        if r.status_code in {401,403}:raise ConfigurationBlocked(f'GEO Engine advance POST authorization denied: HTTP {r.status_code}')
        if r.status_code>=400:raise ConfigurationBlocked(f'GEO Engine advance POST failed: HTTP {r.status_code}')
        return r.json()
    def start(self,key,payload):
        existing=self.reconcile(key)
        if existing:return existing
        try:r=self.client.post('/api/v1/integration/outreach-jobs',headers={'Idempotency-Key':key},json=payload)
        except httpx.TransportError as e:raise OutcomeUncertain('Engine start response uncertain; reconcile the stable job key before retrying') from e
        if r.status_code in {401,403}:raise ConfigurationBlocked(f'GEO Engine job start POST authorization denied: HTTP {r.status_code}')
        if r.status_code in {404,422}:raise ConfigurationBlocked(f'GEO Engine job start POST rejected configuration: HTTP {r.status_code}')
        if r.status_code>=400:raise ConfigurationBlocked(f'GEO Engine job start POST failed: HTTP {r.status_code}')
        return r.json()

GRAPH_PROPERTY='String {e58879bb-e8b5-4fa4-8fa4-bfaef406c054} Name OutreachDeliveryKey'
class GraphReports:
    """Mailbox-authorized app-only Graph; report delivery only, never invitations.

    Draft IDs and extended properties support reconciliation. A lost create/send
    response is never interpreted as permission to create another message.
    """
    def __init__(self):
        names=['OUTREACH_GRAPH_TENANT_ID','OUTREACH_GRAPH_CLIENT_ID','OUTREACH_GRAPH_CLIENT_SECRET','OUTREACH_REPORT_SENDER']
        if not all(os.environ.get(n) for n in names):raise ConfigurationBlocked('Authorized unattended report sender is not configured')
        tenant,client,secret,self.sender=[os.environ[n] for n in names]
        r=httpx.post(f'https://login.microsoftonline.com/{quote(tenant,safe="")}/oauth2/v2.0/token',data={'client_id':client,'client_secret':secret,'grant_type':'client_credentials','scope':'https://graph.microsoft.com/.default'},timeout=30)
        if r.status_code!=200:raise ConfigurationBlocked('Microsoft application authorization failed')
        self.client=httpx.Client(base_url='https://graph.microsoft.com/v1.0',headers={'Authorization':'Bearer '+r.json()['access_token'],'Prefer':'IdType="ImmutableId"'},timeout=30)
        self.mailbox='/users/'+quote(self.sender,safe='')
        self.subject='Your GEO website research report'
        self.body='Thank you for participating in the GEO research study. Your evidence-based website optimization report is attached. The report describes the observed findings, recommendations and limitations.\n\nBest regards\nGEO Research Outreach'
        self.reply_to=None
    def configure_message(self,subject,body,sender=None,*,reply_to=None,sender_name=None):
        if sender and sender.lower()!=self.sender.lower():
            raise ConfigurationBlocked('Configured report sender does not match the authorized mailbox')
        if not subject.strip() or not body.strip():raise ConfigurationBlocked('Configured report email template is empty')
        self.subject=subject.strip();self.body=body.strip();self.reply_to=reply_to or None
    def reconcile(self,key):
        value=key.replace("'","''")
        r=self.client.get(self.mailbox+'/messages',params={'$filter':f"singleValueExtendedProperties/Any(ep: ep/id eq '{GRAPH_PROPERTY}' and ep/value eq '{value}')",'$select':'id,isDraft,sentDateTime,internetMessageId,toRecipients'})
        if r.status_code in {401,403}:raise ConfigurationBlocked('Report mailbox lacks authorized Mail.ReadWrite/Mail.Send access')
        r.raise_for_status();rows=r.json().get('value',[])
        if len(rows)>1:raise OutcomeUncertain('Multiple provider messages share the delivery key; no resend attempted')
        return rows[0] if rows else None
    def create(self,key,recipient,document):
        existing=self.reconcile(key)
        if existing:return existing
        payload={'subject':self.subject,'body':{'contentType':'Text','content':self.body},
                 'toRecipients':[{'emailAddress':{'address':recipient}}],
                 'singleValueExtendedProperties':[{'id':GRAPH_PROPERTY,'value':key}],
                 'attachments':[{'@odata.type':'#microsoft.graph.fileAttachment','name':'GEO-research-report.docx','contentType':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','contentBytes':base64.b64encode(document).decode()}]}
        if self.reply_to:
            payload['replyTo']=[{'emailAddress':{'address':self.reply_to}}]
        try:r=self.client.post(self.mailbox+'/messages',json=payload)
        except httpx.TransportError as e:raise OutcomeUncertain('Graph draft creation outcome is uncertain') from e
        if r.status_code in {401,403}:raise ConfigurationBlocked('Report mailbox permission denied')
        if r.status_code>=500:raise OutcomeUncertain('Graph draft creation outcome is uncertain')
        r.raise_for_status();return r.json()
    def send(self,message_id):
        try:r=self.client.post(self.mailbox+'/messages/'+quote(message_id,safe='')+'/send')
        except httpx.TransportError as e:raise OutcomeUncertain('Graph send outcome is uncertain; reconcile before any resend') from e
        if r.status_code!=202:
            if r.status_code in {401,403}:raise ConfigurationBlocked('Report sending permission denied')
            raise OutcomeUncertain(f'Graph send response HTTP {r.status_code}; reconcile saved message')
        return {'status':'ACCEPTED','provider_message_id':message_id}
