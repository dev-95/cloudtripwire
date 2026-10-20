import tripwire


def test_package_imports() -> None:
    assert tripwire.__name__ == "tripwire"
