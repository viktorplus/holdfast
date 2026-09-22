# Checks

Generated from the register by `holdfast audit --coverage --markdown`.
Editing this file by hand achieves nothing: a test compares it with
what the code actually registers.

| id | group | technique | severity | what it answers |
| --- | --- | --- | --- | --- |
| `authorized_keys` | Access | T1098.004 | high | Keys in authorized_keys |
| `effective_sshd_config` | Access | T1078 | high | Effective sshd configuration |
| `fail2ban_jail` | Access | T1110 | medium | fail2ban and the port sshd actually listens on |
| `pending_security_updates` | Access | T1190 | medium | Packages left unpatched |
| `shell_users` | Access | T1078 | low | Accounts with a shell |
| `ssh_password_auth` | Access | T1110 | high | Password authentication over SSH |
| `ssh_root_login` | Access | T1078 | high | Root login over SSH |
| `docker_published_ports` | Network | T1133 | high | Ports Docker published to the world |
| `established_connections` | Network | T1071 | medium | Outbound connections in progress |
| `external_db_reachable` | Network | T1133 | high | Databases reachable from outside |
| `external_egress` | Network | T1041 | high | What the application container can reach |
| `external_open_ports` | Network | T1133 | high | Ports visible from the internet |
| `origin_reachable_directly` | Network | T1133 | high | The origin answering past the service in front of it |
| `redis_without_password` | Network | T1190 | high | A key-value store with no password |
| `env_permissions` | Secrets and permissions | T1552.001 | high | Permissions on files holding secrets |
| `plaintext_in_app_logs` | Secrets and permissions | T1552.001 | medium | Secrets reaching the application log |
| `private_key_on_host` | Secrets and permissions | T1552.004 | high | Keys that open the backups, on the server |
| `secrets_in_scripts` | Secrets and permissions | T1552.001 | high | Live values written into scripts |
| `world_writable` | Secrets and permissions | T1222 | medium | Files anyone can write to |
| `admin_accounts` | Application | T1136 | high | Who administers the application |
| `app_debug_mode` | Application | T1592 | high | Debug mode in production |
| `dev_packages_installed` | Application | T1592 | high | Debug packages in a live deployment |
| `dev_routes_reachable` | Application | T1190 | high | Debug routes from outside |
| `http_endpoint` | Application | T1499 | high | Whether the site answers |
| `server_denies_sensitive` | Application | T1592 | high | The web server refusing to serve dumps and environment files |
| `tls_certificate` | Application | T1588.004 | medium | How long the certificates last |
| `web_root_extra_files` | Application | T1592 | high | Files under the document root that should not be there |
| `account_changes` | Traces of intrusion | T1136 | high | Changes to accounts |
| `authorized_keys_times` | Traces of intrusion | T1098.004 | high | When authorized_keys was last touched |
| `config_drift` | Traces of intrusion | T1565.001 | high | Drift in the live configuration |
| `container_temp` | Traces of intrusion | T1505.003 | high | The temporary directory inside each container |
| `cpu_anomaly` | Traces of intrusion | T1496 | medium | Load that has been sustained for hours |
| `critical_file_hashes` | Traces of intrusion | T1565.001 | high | Changes in the files that matter |
| `datastore_persistence` | Traces of intrusion | T1505 | high | A data store used to hold a foothold |
| `executables_in_temp` | Traces of intrusion | T1036 | medium | New executables in a temporary directory |
| `known_indicators` | Traces of intrusion | T1204.002 | high | Indicators from a supplied list |
| `ld_so_preload` | Traces of intrusion | T1574.006 | high | Preloaded libraries |
| `obfuscated_payload` | Traces of intrusion | T1027 | high | Payloads written to be unreadable |
| `process_from_temp` | Traces of intrusion | T1059 | high | Processes running out of a temporary directory |
| `scheduled_jobs` | Traces of intrusion | T1053 | high | cron and systemd timers |
| `successful_logins` | Traces of intrusion | T1078 | high | Successful SSH logins |
| `suid_files` | Traces of intrusion | T1548.001 | high | SUID outside the system paths |
| `suspicious_process` | Traces of intrusion | T1059 | high | Command lines that match a known shape |
| `docker_log_rotation` | Logs and monitoring | T1565.001 | low | A size limit on container logs |
| `journald_limit` | Logs and monitoring | T1565.001 | low | A size limit on the journal |
| `log_retention_depth` | Logs and monitoring | T1070.002 | medium | How far back the logs go |
| `remote_log_collector` | Logs and monitoring | T1070.002 | high | Logs leaving this machine |
| `backup_encrypted` | Backups | T1005 | high | Whether the newest snapshot needs a key to read |
| `backup_freshness` | Backups | T1490 | high | How old the newest snapshot is |
| `backup_offsite_copy` | Backups | T1490 | high | Whether a second copy exists somewhere else |
| `restore_tested` | Backups | T1490 | high | Whether a restore has ever been carried out |
| `storage_credentials_scope` | Backups | T1078 | medium | Whether each machine has its own storage account |

52 checks.
