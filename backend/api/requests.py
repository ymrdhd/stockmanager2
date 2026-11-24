"""Read-only recovery endpoint: did a specific request already reach the ledger?"""

import json

from fastapi import APIRouter, Request

from backend.api.trading import invoke, service, wire

router = APIRouter(prefix='/api/v1', tags=['requests'])


@router.get('/requests/{request_id}')
def request_state(request_id: str, request: Request):
    """Report whether a write request is already committed, without changing any fact.

    The ledger records a request only inside the transaction that committed it, so an
    unrecorded identifier is genuinely unverified and may be retried with identical
    content; the stored fingerprint rejects a retry that changed the content.
    """
    def lookup() -> dict:
        db = service(request).db
        with db.read() as conn:
            row = conn.execute('SELECT result_json FROM ledger_requests WHERE request_id=?',
                               (request_id,)).fetchone()
            identity = db.identity
        return {
            'request_id': request_id,
            'committed': row is not None,
            'result': wire(json.loads(row[0])) if row is not None else None,
            'database_uuid': identity.database_uuid,
            'generation': identity.generation,
            'instance_role': identity.instance_role,
            'writable': db.settings.writable,
        }
    return invoke(lookup)
