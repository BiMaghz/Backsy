# Backsy
a modular, lightweight Python backup tool for files and databases — local or remote via SSH — that can send backups to multiple destinations such as Cloudflare (temporary 24-hour download links), Telegram, or Discord. It supports database dumps (MySQL / MariaDB and PostgreSQL, including Dockerized instances), allows excluding files or paths, and uses fast synchronization and compression tools (e.g., rsync, pigz) and parallel processing for efficient, reliable backups.

---

### 🚀 Quick Start

Run the following command as **root** to install and configure :

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/BiMaghz/Backsy/main/install.sh)
```
After installation, you can run the same command again to access the management menu.

### 🔧 Setting Up the Cloudflare Worker

To use the Cloudflare destination, you need to set up a free Cloudflare Worker and a KV namespace.

1.  **Create the Worker:**
    -   Log in to your Cloudflare dashboard.
    -   Go to **Workers & Pages** -> **Create application** -> **Create Worker**.
    -   Give your worker a name (e.g., `backsy-uploader`) and click **Deploy**.

2. **Add the Code:**
   - Click **Edit code**.
   - Delete the default "Hello World" code and paste the contents of the [worker.js](https://raw.githubusercontent.com/BiMaghz/Backsy/main/backuptool/cloudflare-worker/worker.js) file
   - Click **Save and deploy**.

3.  **Create a KV Namespace:**
    -   In your worker's settings, go to **Settings** -> **Variables**.
    -   Scroll down to **KV Namespace Bindings** and click **Add binding**.
    -   **Variable name:** `BACKUP_KV`
    -   **KV namespace:** Click the dropdown and select **Create a new namespace**. Give it a name like `BacksyStorage`.

4.  **Set the API Token (Optional but Recommended):**
    -   Still in **Settings** -> **Variables**, scroll to **Environment Variables** and click **Add variable**.
    -   **Variable name:** `API_TOKEN`
    -   **Value:** Enter a strong, random password or token that you will use in your `run_backup.sh` file.
    -   Click **Encrypt** to keep it secure.

5.  **Deploy Again:**
    -   Click **Save and deploy** one last time at the top of the page to apply the variable bindings. Your worker is now ready!