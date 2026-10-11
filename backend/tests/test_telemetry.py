from app.services.telemetry import parse_mega_progress_line, parse_rclone_stats_line


def test_rclone_json_stats_parser_extracts_progress_without_exposing_raw_log():
    line = (
        '{"level":"notice","msg":"Transferred: 1.5 MiB / 3 MiB, 50%, 512 KiB/s, ETA 3s\\n'
        'Checks: 2 / 4, 50%\\nTransferred: 2 / 4, 50%\\nTransferring:\\n'
        ' * /source/report.pdf: 50%\\nElapsed time: 3s",'
        '"time":"2026-10-10T10:15:32Z"}'
    )
    parsed = parse_rclone_stats_line(line)
    assert parsed == {
        "bytes_transferred": 1572864,
        "bytes_total": 3145728,
        "files_completed": 2,
        "files_total": 4,
        "current_speed_bps": 524288,
        "current_item": "/source/report.pdf",
        "estimated_remaining_seconds": 3,
        "progress_percent": 50.0,
    }


def test_rclone_stats_parser_ignores_malformed_and_non_stats_lines():
    assert parse_rclone_stats_line("not json") is None
    assert parse_rclone_stats_line('{"level":"info","msg":"Copied a file"}') is None


def test_mega_progress_parser_returns_only_numeric_transfer_metrics():
    assert parse_mega_progress_line("TRANSFERING ||################||(12/24 MB: 50.00 %)") == {
        "bytes_transferred": 12_000_000,
        "bytes_total": 24_000_000,
        "progress_percent": 50.0,
    }
    assert parse_mega_progress_line("Download finished: /private/name.txt") is None
    assert parse_mega_progress_line("TRANSFERRING ||####||(secret token)") is None
