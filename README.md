# Backsy
> Simple, Secure, and Automated Backup Solution.

## Features

| Category | Description |
| :--- | :--- |
| **Files** | Local or Remote Server (via SSH). Supports **custom include/exclude paths** to filter your data. |
| **Databases** | Supports MySQL, MariaDB and PostgreSQL via **Docker containers** or **native host installations**. |
| **Destinations** | S3-compatible storage, Telegram, or S3 pre-signed temporary download links (shown in Telegram captions). |
| **Monitoring** | Integrated **Healthchecks** (pinging Healthchecks.io or Uptime Kuma) and **Notification Only** mode (send alerts/logs to Telegram without uploading files). |
| **Security** | Secured with **GPG encryption**. |

---

### Quick Start

Run the following command as **root** to install and configure :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/BiMaghz/Backsy/main/install.sh)
```
After installation, you can run the same command again to access the management menu.

---

### Configuration Guide

#### Compression (gzip / zstd)

Archives are gzip-compressed by default (`pigz` when installed). Set `compression.algorithm: zstd` in `config.yml` (or use **Change Compression** in the manager menu) for much faster compression at a similar size; the default is `zstd -3 -T0` (level 3, all cores). `level` and `threads` can be set too. zstd must be installed on this server **and on every remote server**; a server without it falls back to gzip, and its archive is named `.tar.gz` instead of `.tar.zst`.

Restoring a zstd archive: `tar -I zstd -xf backup.tar.zst` (decrypt `.gpg` files first).

#### Remote Targets (SSH host keys)

Backsy only connects to servers whose SSH host key it already knows. The setup wizard shows the server's fingerprints and asks you to confirm them; they are stored in `/opt/Backsy/known_hosts`. For targets configured earlier (or after a server is reinstalled), use **Trust SSH Host Keys** in the manager menu. A *changed* key is reported as a possible man-in-the-middle and the target is skipped.

#### S3 Storage (R2, ArvanCloud, AWS)

You can configure **any number of S3 destinations**; every backup is uploaded to all enabled ones at the same time. Each destination has a name of your choice (it appears in logs and alerts as `s3:<name>`):

```yaml
services:
  s3:
    primary:
      endpoint_url: https://<ACCOUNT_ID>.r2.cloudflarestorage.com
      bucket_name: backups
      access_key: ${S3_PRIMARY_ACCESS_KEY}
      secret_key: ${S3_PRIMARY_SECRET_KEY}
      generate_link: true          # 24h pre-signed link in the Telegram message
      prefix: backsy/              # optional folder inside the bucket
    offsite:
      endpoint_url: https://s3.ir-thr-at1.arvanstorage.ir
      bucket_name: backups-offsite
      access_key: ${S3_OFFSITE_ACCESS_KEY}
      secret_key: ${S3_OFFSITE_SECRET_KEY}
      addressing_style: path       # auto (default) | path | virtual
```

* **Endpoint URL:** e.g., `https://s3.ir-thr-at1.arvanstorage.ir` or `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
* **Credentials:** `Access Key` and `Secret Key` (give each destination its own).
* **Bucket Name:** The specific bucket you want to store backups in.
* **`prefix`** (optional): a folder inside the bucket, e.g. `backsy/`.
* **`addressing_style`** (optional): `auto` by default; some providers (e.g. MinIO) need `path`.
* **`enable: false`** switches a destination off without deleting it.
* **Failures:** if any destination fails you get an alert, and the other destinations still receive their copy.
* **Download links:** `generate_link` needs read access to the bucket. Only one link is shown in the Telegram message (the first destination, in config order, whose upload succeeded and has links enabled). Credentials that can only write are fine for destinations without links.
* **Old configs:** a single `s3:` block written directly under `services` (with `enable`, `bucket_name`, ...) still works and is treated as one destination named `default`. Do not mix it with named destinations; the setup wizard and the manager's **Add S3 Destination** convert it for you.

##### Folder layout and retention (optional)

By default archives are stored as `<prefix><archive>`. With `prefix_per_target: true` each target gets its own folder: `<prefix><target>/<archive>`. Existing objects are never moved when you change this.

Retention is **off unless you configure it**. Per destination:

```yaml
      retention:
        keep_last: 14     # always keep the newest 14 backups of each target
        keep_days: 30     # ...and everything newer than 30 days (optional)
        dry_run: true     # only log what would be deleted; set to false once the log looks right
```

* It runs only after the backup was uploaded successfully to that destination, and a retention problem never fails the backup (you get a warning in the log).
* It only ever deletes files that Backsy named (`<timestamp>_<target>.tar.gz|.tar.zst[.gpg]`) for **that target**, directly inside the destination's folder. Other files, sub-folders and other targets are left alone.
* The newest `keep_last` backups and the one just uploaded are always kept; age is read from the timestamp in the file name.
* It needs permission to list and delete objects in the bucket.

**Script retention or bucket lifecycle rules?** Use `retention` when you want "keep the last N" (lifecycle rules can only expire by age) or the same behaviour on every provider. Use the provider's lifecycle rules for destinations whose credentials must be write-only (for example a hardened offsite bucket), or as a hard cap next to `retention`. Be generous with age-based rules: if backups ever stop, an age-based rule will eventually delete every backup you have. Example for AWS S3 (R2, MinIO and others offer similar rules; check what your provider supports):

```json
{"Rules": [{"ID": "expire-old-backups", "Status": "Enabled", "Filter": {"Prefix": "backsy/"}, "Expiration": {"Days": 90}}]}
```

`aws s3api put-bucket-lifecycle-configuration --bucket BUCKET --lifecycle-configuration file://lifecycle.json` (add `--endpoint-url` for non-AWS providers).
