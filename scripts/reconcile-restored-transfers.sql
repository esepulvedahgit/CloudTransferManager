BEGIN;

WITH interrupted_transfers AS (
    UPDATE transfer_jobs
    SET status = 'FAILED',
        dispatch_token = NULL,
        dispatch_claimed_at = NULL,
        execution_heartbeat_at = NULL,
        cancel_requested_at = NULL,
        wait_reason = NULL,
        completed_at = COALESCE(completed_at, now()),
        updated_at = now()
    WHERE status IN ('SCHEDULED', 'QUEUED', 'VALIDATING', 'RUNNING', 'RETRYING', 'CANCELLING')
    RETURNING id
), recovery_events AS (
    INSERT INTO transfer_events (
        id,
        transfer_job_id,
        level,
        event_type,
        message,
        technical_details,
        correlation_id,
        created_at
    )
    SELECT
        gen_random_uuid(),
        id,
        'WARNING',
        'RESTORE_REQUIRES_REVIEW',
        'Transfer state was reconciled after database restore. Verify the provider destination before creating a replacement copy.',
        '{"reason":"database_restore","action":"verify_destination_before_retry"}'::jsonb,
        gen_random_uuid(),
        now()
    FROM interrupted_transfers
    RETURNING transfer_job_id
)
SELECT count(*) FROM recovery_events;

DO $$
BEGIN
    IF to_regclass('public.transfer_notification_outbox') IS NOT NULL THEN
        UPDATE transfer_notification_outbox
        SET status = 'FAILED',
            claim_token = NULL,
            claimed_at = NULL,
            last_error_code = 'RESTORE_REQUIRES_REVIEW',
            updated_at = now()
        WHERE status IN ('PENDING', 'QUEUED', 'SENDING');
    END IF;
END;
$$;

COMMIT;
