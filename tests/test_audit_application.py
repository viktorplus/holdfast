"""The web profile, and the eight application checks that depend on it.

The point of the profile is that an undeclared stack is not quietly assumed to
be a familiar one. Several tests below exist only to hold that line: they
assert UNKNOWN where a guessing implementation would have said PASS.
"""

from __future__ import annotations

from support import context, write

from holdfast.audit import webprofile
from holdfast.audit.checks import application
from holdfast.audit.model import FAIL, PASS, UNKNOWN, WARN

LARAVEL = {"audit.web.framework": "laravel"}
NGINX = {"audit.web.server": "nginx"}
FULL = {**LARAVEL, **NGINX}


def nginx_site(tmp_path, body: str) -> None:
    write(tmp_path, "/etc/nginx/sites-enabled/default", body)


# -- the profile itself -----------------------------------------------------


def test_an_undeclared_framework_is_none(tmp_path):
    assert webprofile.framework(context(tmp_path)) is None


def test_an_unknown_framework_is_also_none(tmp_path):
    ctx = context(tmp_path, **{"audit.web.framework": "something-else"})
    assert webprofile.framework(ctx) is None


def test_the_two_ways_of_being_missing_read_differently():
    silent = webprofile.undeclared("framework", "")
    named = webprofile.undeclared("framework", "something-else")
    assert "no framework is declared" in silent
    assert "not a profile this release knows" in named
    assert silent != named


def test_document_roots_come_from_the_server_config(tmp_path):
    nginx_site(tmp_path, "server {\n  root /srv/site/public;\n}\n")
    assert webprofile.web_roots(context(tmp_path, **NGINX)) == ["/srv/site/public"]


def test_declared_roots_are_used_when_the_server_is_not_readable(tmp_path):
    ctx = context(tmp_path, **{"audit.web.roots": ["/srv/site"]})
    assert webprofile.web_roots(ctx) == ["/srv/site"]


def test_var_www_is_the_last_resort(tmp_path):
    (tmp_path / "var" / "www").mkdir(parents=True)
    assert webprofile.web_roots(context(tmp_path)) == ["/var/www"]


# -- app_debug_mode ---------------------------------------------------------


def test_debug_mode_without_a_framework_is_unknown(tmp_path):
    write(tmp_path, "/var/www/app/.env", "APP_DEBUG=true\n")
    status, detail, _ = application._app_debug_mode(context(tmp_path))
    assert status == UNKNOWN
    assert "no framework is declared" in detail


def test_debug_switched_on_fails(tmp_path):
    write(tmp_path, "/var/www/app/.env", "APP_ENV=production\nAPP_DEBUG=true\n")
    status, detail, _ = application._app_debug_mode(context(tmp_path, **LARAVEL))
    assert status == FAIL
    assert "APP_DEBUG=true" in detail


def test_a_non_production_environment_fails(tmp_path):
    write(tmp_path, "/var/www/app/.env", "APP_ENV=local\nAPP_DEBUG=false\n")
    status, detail, _ = application._app_debug_mode(context(tmp_path, **LARAVEL))
    assert status == FAIL
    assert "APP_ENV=local" in detail


def test_production_with_debug_off_passes(tmp_path):
    write(tmp_path, "/var/www/app/.env", "APP_ENV=production\nAPP_DEBUG=false\n")
    status, detail, _ = application._app_debug_mode(context(tmp_path, **LARAVEL))
    assert status == PASS
    assert "1 environment file" in detail


def test_no_environment_file_at_all_is_unknown(tmp_path):
    (tmp_path / "var" / "www").mkdir(parents=True)
    assert application._app_debug_mode(context(tmp_path, **LARAVEL))[0] == UNKNOWN


# -- dev_packages_installed -------------------------------------------------


def test_debug_packages_without_a_framework_are_unknown(tmp_path):
    assert application._dev_packages_installed(context(tmp_path))[0] == UNKNOWN


def test_a_debug_package_directory_warns(tmp_path):
    (tmp_path / "var/www/vendor/laravel/telescope").mkdir(parents=True)
    status, detail, _ = application._dev_packages_installed(
        context(tmp_path, **LARAVEL)
    )
    assert status == WARN
    assert "telescope" in detail


def test_no_debug_package_passes(tmp_path):
    (tmp_path / "var" / "www").mkdir(parents=True)
    assert application._dev_packages_installed(context(tmp_path, **LARAVEL))[0] == PASS


# -- dev_routes_reachable ---------------------------------------------------


def test_the_routes_check_names_the_stack_routes(tmp_path):
    status, _, manual = application._dev_routes_reachable(context(tmp_path, **LARAVEL))
    assert status == UNKNOWN
    assert "_ignition" in manual


def test_the_routes_check_without_a_framework_says_which_way_it_is_missing(tmp_path):
    status, detail, _ = application._dev_routes_reachable(context(tmp_path))
    assert status == UNKNOWN
    assert "no framework is declared" in detail


# -- web_root_extra_files ---------------------------------------------------


def test_a_dump_under_the_document_root_fails(tmp_path):
    nginx_site(tmp_path, "server {\n  root /var/www/app;\n}\n")
    write(tmp_path, "/var/www/app/backup.sql", "-- dump\n")
    status, detail, _ = application._web_root_extra_files(context(tmp_path, **NGINX))
    assert status == FAIL
    assert "/var/www/app/backup.sql" in detail


def test_a_git_directory_under_the_document_root_fails(tmp_path):
    nginx_site(tmp_path, "server {\n  root /var/www/app;\n}\n")
    (tmp_path / "var/www/app/.git").mkdir(parents=True)
    status, detail, _ = application._web_root_extra_files(context(tmp_path, **NGINX))
    assert status == FAIL
    assert ".git directory" in detail


def test_a_tidy_document_root_passes(tmp_path):
    nginx_site(tmp_path, "server {\n  root /var/www/app;\n}\n")
    write(tmp_path, "/var/www/app/index.php", "<?php\n")
    assert application._web_root_extra_files(context(tmp_path, **NGINX))[0] == PASS


# -- server_denies_sensitive ------------------------------------------------


DENYING = """
server {
  root /var/www/app;
  location ~ /\\.env { deny all; }
  location ~ /\\.git { deny all; }
  location ~* \\.(sql|zip|tar|tgz)$ { deny all; }
}
"""

MENTIONING = """
server {
  root /var/www/app;
  location ~ /\\.env { try_files $uri =200; }
}
"""


def test_without_a_declared_server_it_is_unknown(tmp_path):
    nginx_site(tmp_path, DENYING)
    status, detail, _ = application._server_denies_sensitive(context(tmp_path))
    assert status == UNKNOWN
    assert "no server is declared" in detail


def test_an_unknown_server_says_so_differently(tmp_path):
    ctx = context(tmp_path, **{"audit.web.server": "caddy"})
    status, detail, _ = application._server_denies_sensitive(ctx)
    assert status == UNKNOWN
    assert "not a profile this release knows" in detail


def test_full_deny_rules_pass(tmp_path):
    nginx_site(tmp_path, DENYING)
    assert application._server_denies_sensitive(context(tmp_path, **NGINX))[0] == PASS


def test_mentioning_a_path_without_denying_it_fails(tmp_path):
    """Naming .env in a location block is not the same as refusing to serve it."""
    nginx_site(tmp_path, MENTIONING)
    status, detail, _ = application._server_denies_sensitive(context(tmp_path, **NGINX))
    assert status == FAIL
    assert "environment files" in detail


def test_no_server_configuration_is_unknown(tmp_path):
    status, detail, _ = application._server_denies_sensitive(context(tmp_path, **NGINX))
    assert status == UNKNOWN
    assert "no nginx configuration" in detail


# -- tls_certificate --------------------------------------------------------


def test_no_certificates_is_unknown(tmp_path):
    assert application._tls_certificate(context(tmp_path))[0] == UNKNOWN


def test_an_unreadable_certificate_is_unknown_not_pass(tmp_path, monkeypatch):
    write(tmp_path, "/etc/letsencrypt/live/site/fullchain.pem", "not a certificate\n")
    monkeypatch.setattr(application, "_certificate_expiry", lambda path: None)
    status, detail, _ = application._tls_certificate(context(tmp_path))
    assert status == UNKNOWN
    assert "do not parse" in detail


def test_a_certificate_expiring_soon_fails(tmp_path, monkeypatch):
    import time

    write(tmp_path, "/etc/letsencrypt/live/site/fullchain.pem", "x\n")
    monkeypatch.setattr(
        application, "_certificate_expiry", lambda path: time.time() + 3 * 86400
    )
    assert application._tls_certificate(context(tmp_path))[0] == FAIL


def test_a_certificate_inside_the_warning_window_warns(tmp_path, monkeypatch):
    import time

    write(tmp_path, "/etc/letsencrypt/live/site/fullchain.pem", "x\n")
    monkeypatch.setattr(
        application, "_certificate_expiry", lambda path: time.time() + 20 * 86400
    )
    assert application._tls_certificate(context(tmp_path))[0] == WARN


def test_a_long_lived_certificate_passes(tmp_path, monkeypatch):
    import time

    write(tmp_path, "/etc/letsencrypt/live/site/fullchain.pem", "x\n")
    monkeypatch.setattr(
        application, "_certificate_expiry", lambda path: time.time() + 200 * 86400
    )
    status, detail, _ = application._tls_certificate(context(tmp_path))
    assert status == PASS
    assert "200 days" in detail


# -- admin_accounts ---------------------------------------------------------


def test_no_command_is_unknown_because_no_schema_is_guessed(tmp_path):
    status, detail, _, _ = application._admin_accounts(context(tmp_path))
    assert status == UNKNOWN
    assert "does not guess" in detail


# -- http_endpoint ----------------------------------------------------------


def test_no_url_means_nothing_is_sent(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("nothing may be sent without a configured URL")

    monkeypatch.setattr(application.urllib.request, "urlopen", refuse)
    status, detail, _ = application._http_endpoint(context(tmp_path))
    assert status == UNKNOWN
    assert "not set" in detail


def test_a_url_without_a_scheme_is_refused(tmp_path):
    ctx = context(tmp_path, **{"audit.http.url": "example.com"})
    status, detail, _ = application._http_endpoint(ctx)
    assert status == UNKNOWN
    assert "http://" in detail
