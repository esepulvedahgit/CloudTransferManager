from app.worker import celery_app


def test_transfer_and_notification_tasks_use_worker_queue():
    routes = celery_app.conf.task_routes

    assert routes["app.worker.run_transfer"]["queue"] == "transfers"
    assert routes["app.worker.send_transfer_notification"]["queue"] == "transfers"
    assert celery_app.conf.task_default_queue == "transfers"
