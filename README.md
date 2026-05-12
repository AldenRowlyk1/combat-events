# Combat Robotics Event Tracker for Discord

This starter project checks combat robotics event pages and posts nearby upcoming events into a Discord channel using a webhook.

## What it tracks

Default sources:
- RobotCombatEvents event map
- BuildersDB upcoming events

Default home point:
- Embry-Riddle Aeronautical University, Daytona Beach, FL

Default radius:
- 450 miles

## Setup

### 1. Create the Discord webhook

In Discord:

1. Open your server.
2. Go to **Server Settings**.
3. Go to **Integrations**.
4. Open **Webhooks**.
5. Create a webhook for a channel like `#combat-events`.
6. Copy the webhook URL.

Do not post the webhook URL publicly.

### 2. Create a GitHub repository

Create a new GitHub repo, then upload these files into it.

### 3. Add the webhook as a GitHub secret

In your GitHub repo:

1. Go to **Settings**.
2. Go to **Secrets and variables**.
3. Go to **Actions**.
4. Create a new repository secret:
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: your Discord webhook URL

### 4. Run it manually once

Go to the **Actions** tab, open **Combat Robotics Event Tracker**, and click **Run workflow**.

### 5. Let it run automatically

The included workflow runs once per day.

## Changing the radius

Edit `.github/workflows/event-tracker.yml`:

```yaml
RADIUS_MILES: "450"
```

Suggested values:

- `250`: mostly Florida
- `450`: Florida + nearby Southeast
- `800`: larger road-trip radius
- `9999`: all events found

## Changing the home location

Edit `event_tracker.py`:

```python
HOME_LAT = 29.1900
HOME_LON = -81.0480
```

## Adding more sources

Add new source URLs in `event_tracker.py` inside `SOURCES`.

## Notes

This is a scraper-based starter. If a website changes its layout, the parser may need a small update.

The tracker stores already-posted events in `posted_events.json`. GitHub Actions commits that file back to the repo so the same event is not posted every day.
