from workflow_tools.ios import _build_cache_dir, _refresh_build_checkout


def test_build_cache_is_scoped_to_toolchain(project):
    first = _build_cache_dir(project, {"version": "Xcode 27.1", "developer_dir": "/Applications/Xcode.app"})
    second = _build_cache_dir(project, {"version": "Xcode 27.2", "developer_dir": "/Applications/Xcode.app"})

    assert first != second
    assert first.is_relative_to(project.state)


def test_refresh_checkout_preserves_sources_and_skips_old_ipas(project):
    sources = project.root / "We Wallet"
    sources.mkdir()
    (sources / "App.swift").write_text("struct App {}\n")
    (project.root / "PocketDuo-1.0-26.ipa").write_bytes(b"old build")
    checkout = project.state / "cache/checkout"

    _refresh_build_checkout(project, checkout)

    assert (checkout / "We Wallet/App.swift").read_text() == "struct App {}\n"
    assert not (checkout / "PocketDuo-1.0-26.ipa").exists()

    (sources / "App.swift").write_text("struct App { let updated = true }\n")
    _refresh_build_checkout(project, checkout)

    assert (checkout / "We Wallet/App.swift").read_text() == "struct App { let updated = true }\n"
    assert (project.root / "PocketDuo-1.0-26.ipa").read_bytes() == b"old build"
