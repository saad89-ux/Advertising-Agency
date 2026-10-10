from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_env_example_lists_local_service_and_model_settings():
    content = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")

    assert "DATABASE_URL=" in content
    assert "REDIS_URL=" in content
    assert "MODEL_API_KEY=" in content
    assert "STORAGE_DIR=storage" in content


def test_gitignore_ignores_local_env_and_storage_but_not_example():
    patterns = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert ".env" in patterns
    assert "/storage/" in patterns
    assert ".env.example" not in patterns
