const TARGET_CAMPAIGN = 'screened-v2-a429c6055896ef7a';
const TARGET_AUTHORIZATION = 'personalization-auth-3deb7a626ac14f67bd7d31a060c6e4e6';
const TARGET_MANIFEST = 'ff3bee5be6fe8370b5484eb4472e8a9d3d88be721b180d2eb2038599dcb7c0c1';
const TARGET_TRIGGER = 'outreach-historical-screening-recovery';

export default {
  async fetch(request: Request): Promise<Response> {
    if (request.method !== 'POST') {
      return Response.json({ error: 'Method not allowed' }, { status: 405 });
    }

    // Neon strips client-supplied X-Neon-* headers at its edge. The function
    // accepts only a genuine scheduled trigger, never arbitrary public calls.
    const invocationId = request.headers.get('x-neon-trigger-invocation-id') || '';
    if (!invocationId) {
      return Response.json({ error: 'Neon trigger authentication required' }, { status: 401 });
    }

    let envelope: any;
    try {
      envelope = await request.json();
    } catch {
      return Response.json({ error: 'Invalid trigger payload' }, { status: 400 });
    }
    if (envelope?.trigger?.type !== 'schedule'
        || envelope?.trigger?.name !== TARGET_TRIGGER
        || envelope?.invocation_id !== invocationId
        || typeof envelope?.data?.scheduled_at !== 'string') {
      return Response.json({ error: 'Unexpected trigger or invocation mismatch' }, { status: 400 });
    }

    const baseUrl = process.env.OUTREACH_STAGING_URL?.replace(/\/$/, '');
    const token = process.env.OUTREACH_HISTORICAL_CAMPAIGN_SCHEDULER_TOKEN;
    if (!baseUrl?.startsWith('https://') || !token || token.length < 32) {
      console.error('Recovery target configuration is incomplete.');
      return Response.json({ error: 'Recovery target is not configured' }, { status: 503 });
    }

    const response = await fetch(`${baseUrl}/api/internal/historical-screening-recovery-tick`, {
      method: 'POST',
      headers: {
        authorization: `Bearer ${token}`,
        'content-type': 'application/json',
        'x-neon-trigger-invocation-id': invocationId,
      },
      body: JSON.stringify({
        trigger_name: TARGET_TRIGGER,
        invocation_id: invocationId,
        scheduled_at: envelope.data.scheduled_at,
        campaign_id: TARGET_CAMPAIGN,
        authorization_id: TARGET_AUTHORIZATION,
        manifest_sha256: TARGET_MANIFEST,
      }),
      signal: AbortSignal.timeout(120_000),
    });

    const result = await response.text();
    if (!response.ok) {
      console.error(`Recovery tick returned HTTP ${response.status}; invocation ${invocationId}.`);
      return Response.json({ error: 'Recovery tick failed', status: response.status }, { status: 502 });
    }

    console.log(`Recovery tick accepted; invocation ${invocationId}.`);
    return new Response(result, {
      status: 200,
      headers: { 'content-type': 'application/json', 'cache-control': 'no-store' },
    });
  },
};
