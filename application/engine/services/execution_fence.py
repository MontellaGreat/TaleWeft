"""A SQL fence is evaluated by the writer, after any queue delay."""


def autopilot_write_fence(novel_id, identity):
    if not isinstance(identity, dict) or any(identity.get(k) is None for k in ('job_id','run_epoch','executor_pid')):
        raise RuntimeError('没有本轮执行身份，拒绝写入')
    return """EXISTS (SELECT 1 FROM novels n
        JOIN novel_execution_leases l ON l.novel_id=n.id
        JOIN autopilot_commands c ON c.command_id=l.job_id AND c.novel_id=n.id
        WHERE n.id=? AND n.autopilot_status='running' AND n.autopilot_run_epoch=?
            AND l.owner='autopilot' AND l.job_id=?
            AND l.expires_at>(julianday('now')-2440587.5)*86400.0
            AND c.state='completed' AND c.command_type IN ('start','resume')
            AND c.run_epoch=n.autopilot_run_epoch AND c.executor_pid=?)""", (
        str(novel_id), identity['run_epoch'], identity['job_id'], identity['executor_pid'])
