from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_windows_deployment_contains_no_docker_setup_run_and_check_commands() -> None:
    document = (ROOT / "WINDOWS_DEPLOY.md").read_text(encoding="utf-8")

    assert "setup_windows_builtin_oauth.ps1" in document
    assert "run_windows_builtin_oauth.ps1 -Port 8005" in document
    assert "check_windows_oauth.ps1" in document
    assert "https://githubaction.giize.com/gemini_mcp/mcp" in document
    assert "复制重定向 URI" in document
    assert "不需要 Docker" in document


def test_windows_caddy_fragment_routes_required_well_known_and_prefixed_paths() -> None:
    fragment = (ROOT / "deploy" / "windows" / "Caddyfile.gemini_mcp.fragment").read_text(encoding="utf-8")

    assert "/.well-known/oauth-protected-resource/gemini_mcp/mcp" in fragment
    assert "rewrite * /.well-known/oauth-protected-resource/mcp" in fragment
    assert "/.well-known/oauth-authorization-server/gemini_mcp" in fragment
    assert "rewrite * /.well-known/oauth-authorization-server" in fragment
    assert "handle_path /gemini_mcp/*" in fragment
    assert "reverse_proxy 127.0.0.1:8005" in fragment


def test_windows_setup_scripts_never_embed_real_credentials() -> None:
    setup_python = (ROOT / "scripts" / "setup_windows_builtin_oauth.py").read_text(encoding="utf-8")
    setup_powershell = (ROOT / "scripts" / "setup_windows_builtin_oauth.ps1").read_text(encoding="utf-8")

    assert "getpass.getpass" in setup_python
    assert "OAuth client secret: saved only" in setup_python
    assert "icacls.exe" in setup_powershell
    assert '"${env:USERNAME}:(OI)(CI)F"' in setup_powershell
    assert "--host 0.0.0.0" not in (ROOT / "scripts" / "run_windows_builtin_oauth.ps1").read_text(encoding="utf-8")
