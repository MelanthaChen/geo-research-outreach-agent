"""Real authenticated transports. Missing configuration is an explicit block."""
from __future__ import annotations
import os,json,base64
from urllib.parse import quote
import httpx
from .consent import REQUIRED_HEADERS
from outreach_agent.google_forms_integration import RESPONSE_SPREADSHEET_ID

class ConfigurationBlocked(RuntimeError):pass
class OutcomeUncertain(RuntimeError):pass

class Sheets:
    def read(self):
        from google.oauth2 import service_account
        from google.auth.transport.requests import AuthorizedSession
        encoded=os.environ.get('GEO_RESEARCH_SERVICE_ACCOUNT_JSON_B64')
        path=os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
        if encoded:
            credentials=service_account.Credentials.from_service_account_info(json.loads(base64.b64decode(encoded)),scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        elif path:
            credentials=service_account.Credentials.from_service_account_file(path,scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        else:raise ConfigurationBlocked('Authenticated Google Sheets service account is not configured')
        session=AuthorizedSession(credentials)
        range_name=os.environ.get('GEO_RESEARCH_SHEETS_RANGE','Form Responses 1!A:Z')
        response=session.get(f'https://sheets.googleapis.com/v4/spreadsheets/{RESPONSE_SPREADSHEET_ID}/values/{quote(range_name,safe="")}',timeout=30)
        if response.status_code!=200:raise ConfigurationBlocked(f'Google Sheets returned HTTP {response.status_code}')
        rows=response.json().get('values') or []
        if not rows or not REQUIRED_HEADERS.issubset(rows[0]):raise ConfigurationBlocked('Live Sheet headers do not match the independent consent and identity contract')
        return [dict(zip(rows[0],row)) for row in rows[1:]]

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
            if caps.status_code!=200 or not caps.json().get('durable_jobs'):raise ConfigurationBlocked('Deployed GEO Engine does not provide durable outreach jobs')
            return None
        if r.status_code in {401,403}:raise ConfigurationBlocked('GEO Engine service authorization failed')
        r.raise_for_status();return r.json()
    def start(self,key,payload):
        existing=self.reconcile(key)
        if existing:return existing
        try:r=self.client.post('/api/v1/integration/outreach-jobs',headers={'Idempotency-Key':key},json=payload)
        except httpx.TransportError as e:raise OutcomeUncertain('Engine start response uncertain; reconcile the stable job key before retrying') from e
        if r.status_code in {401,403,404,422}:raise ConfigurationBlocked(f'GEO Engine rejected research configuration: HTTP {r.status_code}')
        r.raise_for_status();return r.json()

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
        payload={'subject':'Your GEO website research report','body':{'contentType':'Text','content':'Thank you for participating in the GEO research study. Your evidence-based website optimization report is attached. The report describes the observed findings, recommendations and limitations.\n\nBest regards\nGEO Research Outreach'},
                 'toRecipients':[{'emailAddress':{'address':recipient}}],
                 'singleValueExtendedProperties':[{'id':GRAPH_PROPERTY,'value':key}],
                 'attachments':[{'@odata.type':'#microsoft.graph.fileAttachment','name':'GEO-research-report.docx','contentType':'application/vnd.openxmlformats-officedocument.wordprocessingml.document','contentBytes':base64.b64encode(document).decode()}]}
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
