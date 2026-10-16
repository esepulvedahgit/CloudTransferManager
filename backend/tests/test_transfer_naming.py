import importlib.util


def test_allocator_numbers_case_insensitive_collisions_and_keeps_extensions():
    spec = importlib.util.find_spec("app.services.transfer_naming")
    assert spec is not None, "shared destination name allocation is not implemented"
    from app.services.transfer_naming import allocate_available_name

    assert allocate_available_name(
        "Report.TXT",
        {"report.txt", "Report_01.txt", "REPORT_02.TXT"},
    ) == "Report_03.TXT"


def test_allocator_normalizes_unicode_and_handles_hidden_files():
    spec = importlib.util.find_spec("app.services.transfer_naming")
    assert spec is not None, "shared destination name allocation is not implemented"
    from app.services.transfer_naming import allocate_available_name

    assert allocate_available_name("café.txt", {"cafe\u0301.TXT"}) == "café_01.txt"
    assert allocate_available_name(".env", {".env"}) == ".env_01"


def test_allocator_grows_suffix_beyond_two_digits():
    spec = importlib.util.find_spec("app.services.transfer_naming")
    assert spec is not None, "shared destination name allocation is not implemented"
    from app.services.transfer_naming import allocate_available_name

    occupied = {"archive.zip", *(f"archive_{index:02d}.zip" for index in range(1, 101))}

    assert allocate_available_name("archive.zip", occupied) == "archive_101.zip"


def test_allocator_truncates_base_without_dropping_extension_or_suffix():
    spec = importlib.util.find_spec("app.services.transfer_naming")
    assert spec is not None, "shared destination name allocation is not implemented"
    from app.services.transfer_naming import allocate_available_name

    result = allocate_available_name("é" * 10 + ".txt", {"é" * 10 + ".txt"}, 18)

    assert result.endswith("_01.txt")
    assert len(result.encode("utf-8")) <= 18


def test_allocator_truncates_an_unoccupied_component_that_exceeds_provider_limit():
    spec = importlib.util.find_spec("app.services.transfer_naming")
    assert spec is not None, "shared destination name allocation is not implemented"
    from app.services.transfer_naming import allocate_available_name

    result = allocate_available_name("é" * 10 + ".txt", set(), 18)

    assert result.endswith(".txt")
    assert len(result.encode("utf-8")) <= 18
