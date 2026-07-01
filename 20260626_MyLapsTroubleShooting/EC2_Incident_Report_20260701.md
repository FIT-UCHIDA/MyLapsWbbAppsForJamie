# EC2 Web Server — Incident Report & Migration Plan

**Date:** 2026-07-01  
**Prepared by:** Kazuo Uchida (FIT)  
**For:** Jamie  

---

## 1. Executive Summary

The web application server became unresponsive on 2026-07-01 (and previously on 2026-06-29), causing the site to be unreachable. Root cause was identified as an **out-of-memory (OOM) condition** triggered by an automatic background process (`dnf` package update checker) consuming over 350 MB of RAM on a 1 GB instance. Immediate fixes have been applied. A planned migration to a larger instance type (t3.small, 2 GB RAM) is scheduled to resolve the issue permanently.

---

## 2. Incident Timeline

| Time (JST) | Event |
|---|---|
| 2026-06-29 ~16:00 | Jamie reports website stops working after 4pm |
| 2026-07-01 16:00 | Website unreachable again; SSH unresponsive |
| 2026-07-01 17:00 | EC2 instance rebooted via AWS Console |
| 2026-07-01 17:10 | Services restored (webapp, nginx confirmed active) |
| 2026-07-01 17:30 | Root cause identified via system logs |
| 2026-07-01 18:00 | Immediate fixes applied (see Section 4) |

---

## 3. Root Cause Analysis

### 3.1 What happened

The EC2 instance (t3.micro, **916 MB RAM**) ran out of memory during normal operation. When RAM is exhausted, the Linux kernel's **OOM (Out-of-Memory) Killer** forcibly terminates processes to free memory. In our case, the gunicorn web workers were killed, taking down the web application.

Evidence from system logs:
```
Jul 01 07:01:49  dnf invoked oom-killer
Jul 01 07:01:49  Out of memory: Killed process 1536 (gunicorn)
Jul 01 08:23:14  Out of memory: Killed process 1540 (gunicorn)
```

### 3.2 Memory breakdown (t3.micro, 916 MB total)

| Process | Memory Usage |
|---|---|
| gunicorn workers (webapp) — 2 workers | ~166 MB |
| gunicorn workers (webapp_beta) — 2 workers | ~166 MB |
| Amazon CloudWatch Agent | ~46 MB |
| nginx | ~15 MB |
| SSM Agent, MySQL, systemd, etc. | ~100 MB |
| **Baseline total** | **~493 MB** |
| `dnf` (auto package update check — triggered daily) | **~350 MB spike** |
| **Peak total** | **~843 MB → exceeds 916 MB** |

### 3.3 Why it occurs around 4–5pm JST

The system runs an automatic package-update notification script (`/etc/update-motd.d/70-available-updates`) on a daily timer. This script calls `dnf updateinfo` and `dnf check-release-update`, each consuming significant memory. The timer fires in the late afternoon JST, coinciding with active training sessions when the web app is already under load.

### 3.4 Why SSH also became unresponsive

When the OOM killer fires repeatedly, system resources are severely strained. Even the SSH daemon (`sshd`) can fail to complete the connection handshake, making the instance appear "dead" even though it is technically still running.

---

## 4. Immediate Fixes Applied (2026-07-01)

### 4.1 Swap file added (1 GB)

A 1 GB swap file was created and made persistent. This provides a memory buffer so that when RAM is full, the system uses disk swap instead of killing processes.

```
/swapfile   1 GB   (permanent via /etc/fstab)
```

**Effect:** Even if `dnf` runs and consumes 350 MB, the system now has headroom to absorb the spike without OOM-killing the web workers.

### 4.2 Webapp watchdog cron (every 5 minutes)

A cron job was added to automatically restart the web app if it becomes unresponsive:

```bash
# /etc/cron.d/webapp-watchdog
*/5 * * * * root curl -sf -k https://localhost/ || systemctl restart webapp webapp_beta nginx
```

**Effect:** If the webapp goes down for any reason, it will self-recover within 5 minutes without requiring manual intervention.

### 4.3 Date range validation in app (max 7 days)

A server-side check was added to reject queries with date ranges exceeding 7 days. This prevents accidental heavy queries from consuming excessive memory.

**Effect:** Protects against user error causing memory spikes from large data fetches.

---

## 5. Planned Migration: t3.micro → t3.small

### 5.1 Why t3.small

| | t3.micro (current) | t3.small (planned) |
|---|---|---|
| RAM | 1 GB | **2 GB** |
| vCPU | 2 | 2 |
| Monthly cost (Tokyo) | ~$8/month | ~$17/month |
| `dnf` spike risk | OOM (fatal) | Absorbed (~843MB / 2048MB = 41%) |
| Headroom for growth | None | Comfortable |

With 2 GB RAM, even if `dnf` consumes 350 MB simultaneously with all web workers, total usage (~843 MB) stays well within the available 2 GB.

### 5.2 Migration procedure

1. **Stop** the EC2 instance  
2. **Change instance type** to `t3.small` (AWS Console → Instance Settings)  
3. **Start** the instance  
4. **Verify** services are active (`webapp`, `webapp_beta`, `nginx`)  
5. **Update URL** — the public IP address will change; a new URL will be provided

> ⚠️ **Important:** The current URL (`https://ec2-43-206-155-252.ap-northeast-1.compute.amazonaws.com/`) will change after migration. A new URL will be communicated once the migration is complete.

### 5.3 What is preserved during migration

| Item | Status |
|---|---|
| All application data (database) | ✅ Preserved (EBS storage) |
| User accounts and settings | ✅ Preserved |
| Application code | ✅ Preserved |
| SSL certificate | ✅ No changes required |
| Swap file | ✅ Preserved (on EBS) |
| Watchdog cron | ✅ Preserved |

---

## 6. Pre-Migration Checklist

- [x] Swap file (1 GB) added and persistent
- [x] Webapp watchdog cron configured
- [x] Date range validation added to app
- [ ] `update-motd` dnf calls disabled (in progress)
- [ ] App.py changes committed to git
- [ ] Migration to t3.small

---

## 7. Outstanding Items (Post-Migration)

### 7.1 Disable automatic dnf updates

The `/etc/update-motd.d/70-available-updates` script runs `dnf` daily. Even on t3.small, this is unnecessary overhead and will be disabled after migration.

### 7.2 Lambda health check (external watchdog)

For full reliability, an external monitoring function (AWS Lambda + EventBridge) will be set up to:
- Check the website every 5 minutes from outside the EC2 instance
- Automatically reboot the EC2 if it becomes completely unresponsive
- Send email notification when a recovery action is taken

**Estimated cost: ~$0/month** (within AWS free tier at this usage level)

### 7.3 Decoder sync (hardware)

As discussed separately, the MyLaps decoder timing synchronization issue (GPS PPS distributor) is a separate hardware task to be addressed at the next PST meeting.

---

## 8. Redundancy Question (from Jamie's email, 2026-07-01)

> *"...looking at redundancy..."*

**Current situation:** Single EC2 instance with:
- Automatic restart if webapp crashes (watchdog)
- Nightly reboot to clear any memory leaks (00:00 JST)
- 1 GB swap as memory buffer

**If redundancy means "backup when the website fails":**
The current webapp can be ported to a standalone desktop application. This would allow timing analysis to continue even if the web server is unavailable. Please let us know if this is the priority and we will scope the work.

**If redundancy means "high availability with no downtime":**
This would require a load balancer + multiple EC2 instances, increasing cost significantly (~3-5×). Not recommended at current scale.

---

## 9. Questions / Next Steps

1. **Confirm URL change is acceptable** before we proceed with t3.small migration
2. **Desktop app port** — is this a priority? If so, how urgent?
3. **PST schedule** — confirm date for decoder sync hardware discussion

---

*Report prepared by Kazuo Uchida / FIT — 2026-07-01*
