"""One administrator, opaque durable sessions and CSRF-protected mutations."""
import hashlib,hmac,os,secrets,uuid
from datetime import datetime,timedelta,timezone
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError,InvalidHashError
from sqlalchemy import select,func
from .db import sessions,auth_attempts,admin_users,now

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def expiry(hours=12):return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()

def login(db,email,password,source):
    email=email.lower().strip()
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
    with db.transaction(serialized=True) as c:
        failures=c.scalar(select(func.count()).select_from(auth_attempts).where(auth_attempts.c.source==source,auth_attempts.c.successful.is_(False),auth_attempts.c.created_at>cutoff))
        if failures>=10:raise PermissionError('Too many attempts; retry after 15 minutes')
        row=c.execute(select(admin_users).where(admin_users.c.email==email,admin_users.c.status=='ACTIVE')).mappings().first()
        expected=os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu').lower().strip()
        encoded=(row['password_hash'] if row else os.environ.get('OUTREACH_ADMIN_PASSWORD_HASH',''))
        valid=False
        try:valid=bool(encoded) and PasswordHasher().verify(encoded,password) and bool(row or hmac.compare_digest(email,expected))
        except (VerificationError,InvalidHashError):pass
        c.execute(auth_attempts.insert().values(id=uuid.uuid4().hex,source=source,successful=valid,created_at=now()))
        if valid:
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            c.execute(sessions.insert().values(token_hash=digest(token),csrf_hash=digest(csrf),email=email,expires_at=expiry(),created_at=now()))
            return {'token':token,'csrf':csrf,'email':email,'role':row['role'] if row else 'ADMIN'}
    raise PermissionError('Invalid email or password')

def session(db,token):
    if not token:return None
    with db.transaction() as c:
        row=c.execute(select(sessions).where(sessions.c.token_hash==digest(token),sessions.c.expires_at>now())).mappings().first()
        if not row:return None
        result=dict(row)
        admin=c.execute(select(admin_users.c.role,admin_users.c.status).where(admin_users.c.email==result['email'])).mappings().first()
        if admin and admin['status']!='ACTIVE':return None
        result['role']=admin['role'] if admin else 'ADMIN' if result['email']==os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu').lower().strip() else 'REVOKED'
        return result

def require_role(session, *roles):
    if not session or session.get('role') not in roles:
        raise PermissionError('Administrator role is not permitted for this action')

def csrf_valid(session,value):return bool(value and hmac.compare_digest(session['csrf_hash'],digest(value)))

def logout(db,token):
    with db.transaction() as c:c.execute(sessions.delete().where(sessions.c.token_hash==digest(token)))
