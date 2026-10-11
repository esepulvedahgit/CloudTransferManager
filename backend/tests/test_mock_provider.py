import pytest

from app.providers.mock import MockStorageProvider


@pytest.mark.asyncio
async def test_mock_provider_lists_files_and_directories_at_root():
    provider = MockStorageProvider()

    page = await provider.list_items("/")

    assert [item.name for item in page.items] == ["Documentos", "Proyectos"]
    assert all(item.is_dir for item in page.items)


@pytest.mark.asyncio
async def test_mock_provider_navigates_to_nested_directory():
    provider = MockStorageProvider()

    page = await provider.list_items("/Proyectos")

    assert [(item.name, item.is_dir) for item in page.items] == [
        ("informe.pdf", False),
        ("video_demo.mp4", False),
    ]


@pytest.mark.asyncio
async def test_mock_provider_rejects_path_traversal():
    provider = MockStorageProvider()

    with pytest.raises(ValueError):
        await provider.list_items("../../etc/passwd")
