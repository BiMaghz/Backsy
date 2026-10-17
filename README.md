# Backsy
> Simple, Secure, and Automated Backup Solution.

## Features

| Category | Description |
| :--- | :--- |
| **Files** | Local or Remote Server (via SSH). Supports **custom include/exclude paths** to filter your data. |
| **Databases** | Supports MySQL, MariaDB and PostgreSQL via **Docker containers** or **native host installations**. |
| **Destinations** | S3-compatible storage, Telegram, Cloudflare KV or S3 pre-signed temporary download links (shown in Telegram captions). |
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

#### Cloudflare Worker

1.  **Create Worker:** In Cloudflare Dashboard > **Workers & Pages**, create a new worker.
2.  **Deploy Code:** Paste the contents of [worker.js](https://raw.githubusercontent.com/BiMaghz/Backsy/main/backuptool/cloudflare-worker/worker.js) and deploy.
3.  **KV Namespace:** In Worker Settings > **Variables**, add a KV Namespace binding named `BACKUP_KV` (Create a new namespace if needed).
4.  **API Token (required):** In Variables, add `API_TOKEN` with a strong password (click Encrypt). Use this same token during Backsy setup. A worker without `API_TOKEN` rejects all uploads.
5.  **Redeploy:** Go to the deployments tab and redeploy to apply changes.

#### Compression (gzip / zstd)

Archives are gzip-compressed by default (`pigz` when installed). Set `compression.algorithm: zstd` in `config.yml` (or use **Change Compression** in the manager menu) for much faster compression at a similar size; the default is `zstd -3 -T0` (level 3, all cores). `level` and `threads` can be set too. zstd must be installed on this server **and on every remote server**; a server without it falls back to gzip, and its archive is named `.tar.gz` instead of `.tar.zst`.

Restoring a zstd archive: `tar -I zstd -xf backup.tar.zst` (decrypt `.gpg` files first).

#### Remote Targets (SSH host keys)

Backsy only connects to servers whose SSH host key it already knows. The setup wizard shows the server's fingerprints and asks you to confirm them; they are stored in `/opt/Backsy/known_hosts`. For targets configured earlier (or after a server is reinstalled), use **Trust SSH Host Keys** in the manager menu. A *changed* key is reported as a possible man-in-the-middle and the target is skipped.

#### S3 Storage (R2, ArvanCloud, AWS)

* **Endpoint URL:** e.g., `https://s3.ir-thr-at1.arvanstorage.ir` or `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
* **Credentials:** `Access Key` and `Secret Key`.
* **Bucket Name:** The specific bucket you want to store backups in.