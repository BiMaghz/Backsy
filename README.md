# Backsy
> Simple, Secure, and Automated Backup Solution.

## Features

| Category | Description |
| :--- | :--- |
| **Files** | Local or Remote Server (via SSH). Supports **custom include/exclude paths** to filter your data. |
| **Databases** | **Dockerized** databases including **MySQL, MariaDB, and PostgreSQL**. |
| **Destinations** | S3-compatible storage, Telegram, Cloudflare KV or S3 pre-signed temporary download links (shown in Telegram captions). |
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
4.  **API Token:** In Variables, add `API_TOKEN` with a strong password (click Encrypt). Use this same token during Backsy setup.
5.  **Redeploy:** Go to the deployments tab and redeploy to apply changes.

#### S3 Storage (R2, ArvanCloud, AWS)

* **Endpoint URL:** e.g., `https://s3.ir-thr-at1.arvanstorage.ir` or `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
* **Credentials:** `Access Key` and `Secret Key`.
* **Bucket Name:** The specific bucket you want to store backups in.