"""One administrator, opaque durable sessions and CSRF-protected mutations."""
import hashlib,hmac,os,secrets,uuid
from datetime import datetime,timedelta,timezone
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError,InvalidHashError
from sqlalchemy import select,func
from .db import sessions,auth_attempts,now

def digest(value):return hashlib.sha256(value.encode()).hexdigest()
def expiry(hours=12):return (datetime.now(timezone.utc)+timedelta(hours=hours)).isoformat()

def login(db,email,password,source):
    expected=os.environ.get('OUTREACH_ADMIN_EMAIL','chen.yuxuan9@northeastern.edu').lower()
    encoded=os.environ.get('OUTREACH_ADMIN_PASSWORD_HASH','')
    if not encoded:raise RuntimeError('Administrator password hash is not configured')
    cutoff=(datetime.now(timezone.utc)-timedelta(minutes=15)).isoformat()
    with db.transaction(serialized=True) as c:
        failures=c.scalar(select(func.count()).select_from(auth_attempts).where(auth_attempts.c.source==source,auth_attempts.c.successful.is_(False),auth_attempts.c.created_at>cutoff))
        if failures>=10:raise PermissionError('Too many attempts; retry after 15 minutes')
        valid=False
        try:valid=PasswordHasher().verify(encoded,password) and hmac.compare_digest(email.lower(),expected)
        except (VerificationError,InvalidHashError):pass
        c.execute(auth_attempts.insert().values(id=uuid.uuid4().hex,source=source,successful=valid,created_at=now()))
        if valid:
            token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32)
            c.execute(sessions.insert().values(token_hash=digest(token),csrf_hash=digest(csrf),email=expected,expires_at=expiry(),created_at=now()))
            return {'token':token,'csrf':csrf,'email':expected}
    raise PermissionError('Invalid email or password')

def session(db,token):
    if not token:return None
    with db.transaction() as c:
        row=c.execute(select(sessions).where(sessions.c.token_hash==digest(token),sessions.c.expires_at>now())).mappings().first()
        return dict(row) if row else None

def csrf_valid(session,value):return bool(value and hmac.compare_digest(session['csrf_hash'],digest(value)))

def logout(db,token):
    with db.transaction() as c:c.execute(sessions.delete().where(sessions.c.token_hash==digest(token)))
