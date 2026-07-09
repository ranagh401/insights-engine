# CI/CD Deployment

Two GitHub Actions workflows live in [.github/workflows](.github/workflows):

| Workflow | File | Trigger | Does |
|----------|------|---------|------|
| **CI** | `ci.yml` | push / PR to `main`, manual | install, ruff (advisory), `pytest` |
| **Deploy to VM** | `deploy.yml` | after CI succeeds on `main`, manual | rsync source to the VM over SSH, restart, health check |

The deploy uses **SSH key auth** — no passwords anywhere in the repo or Actions logs.

---

## One-time setup

### 1. Create a deploy key pair (locally)

```powershell
ssh-keygen -t ed25519 -f deploy_key -C "github-actions-deploy" -N '""'
```

This makes `deploy_key` (private) and `deploy_key.pub` (public). **Do not commit either.**

### 2. Authorize the public key on the VM

```powershell
type deploy_key.pub | ssh webadmin@20.244.108.100 "mkdir -p ~/.ssh && chmod 700 ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys"
```

### 3. Make sure the VM has the repo and its `.env`

The deploy **syncs source only** — it never touches `.env` on the VM. So the VM must already have:

- the repo checked out at some path, e.g. `/home/webadmin/ms-insights-portal-crm`
- a working `.env` in that folder
- the venv / dependencies installed (`pip install -e .`)

If it isn't there yet, do it once:

```bash
ssh webadmin@20.244.108.100
git clone <this-repo-url> ~/ms-insights-portal-crm   # or copy the folder up
cd ~/ms-insights-portal-crm
cp /path/to/your/.env .env       # bring over the real secrets
python3 -m venv .venv && .venv/bin/pip install -e .
```

### 4. Add GitHub secrets and variables

Repo → **Settings → Secrets and variables → Actions**.

**Secrets:**

| Name | Value |
|------|-------|
| `VM_HOST` | `20.244.108.100` |
| `VM_USER` | `webadmin` |
| `VM_SSH_KEY` | full contents of the **private** `deploy_key` file |
| `VM_PATH` | absolute repo path on the VM, e.g. `/home/webadmin/ms-insights-portal-crm` |

**Variables:**

| Name | Value |
|------|-------|
| `VM_RESTART_CMD` | how the service restarts, e.g. `sudo systemctl restart ms-insights-crm` or `docker restart ms-insights-crm` |
| `VM_HEALTH_URL` | *(optional)* e.g. `http://localhost:8010/docs` |

> If restart needs `sudo`, give `webadmin` a NOPASSWD sudoers entry for that one command, otherwise the SSH step will hang.

---

## Normal flow

1. Commit and push to `main`.
2. **CI** runs tests.
3. On green, **Deploy** rsyncs the code up, restarts the service, and health-checks it.
4. Manual redeploy anytime: Actions tab → *Deploy to VM* → **Run workflow**.

## Rollback

```bash
ssh webadmin@20.244.108.100 "cd <VM_PATH> && git checkout <previous-commit> && <restart cmd>"
```

(Keep the VM copy as a git clone so rollback is a `git checkout`.)

---

## Notes / caveats

- **Two files changed** in the current fix — `src/ms_renuity_insights_portal/api/pbi/data.py` and
  `src/ms_renuity_insights_portal/dax/query_builder.py`. Deploy ships the whole tree, so both go together.
- `rsync` runs **without `--delete`**, so files that exist only on the VM are kept. If you want the VM to
  mirror the repo exactly, add `--delete` to the sync step — but review first, it removes anything not in git.
- The old `azure-pipelines/` folder (AKS deploy for the Renuity service) is untouched and unused by this flow.
