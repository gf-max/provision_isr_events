# Provision-ISR Events Integration

## Overview
This custom integration for **Home Assistant** allows you to receive motion and alarm events directly from a **Provision-ISR NVR/DVR** using the ONVIF protocol.  
Each camera channel from your NVR will appear in Home Assistant as a **binary sensor**:  
- **ON** → Motion detected  
- **OFF** → No motion  
This makes it possible to trigger **automations, notifications, or actions** in your smart home based on NVR events.

## Features
- Automatic discovery of NVR channels (cameras).  
- One binary sensor per channel (e.g., `binary_sensor.nvr_motion_1`).  
- Real-time updates via ONVIF PullPoint subscription.  
- Configurable auto-off timer (e.g., reset after 3 seconds).  
- Per-channel auto-off customization (`auto_off_map`).  
- Fallback “unknown” sensor if no channels are configured (useful to test events).  
- Multi-NVR support.  

## Installation
1. Copy the `provision_isr_events` folder into `config/custom_components/` in your Home Assistant setup.  
2. Restart Home Assistant.  
3. Go to **Settings → Devices & Services → Add Integration** and search for **Provision-ISR Events**.  
4. Enter your NVR connection details (host, port, username, password).  

## Installation via HACS
1. Open HACS → Integrations → **Custom repositories**
2. Add repository URL: `https://github.com/youraccount/provision_isr_events`
3. Select category: `Integration`
4. Install and restart Home Assistant

## Configuration
The component is configured via the Home Assistant UI. Required fields:
- Host/IP  
- Port (default: 80)  
- Username / Password  

## Options
- **Auto OFF (seconds):** Default time before a motion sensor resets to OFF.  
- **Auto OFF Map:** Optional per-channel configuration. Example:  
  - `5:3,7:2.5` → Channel 5 resets after 3 seconds, Channel 7 after 2.5 seconds.  
- **Channels:** Choose which channels to monitor (e.g., `1,2,6`). Leave empty to include all channels.  

## Example Automations

### 1. Motion → Turn on Light
```yaml
alias: Motion - Entrance Light
trigger:
  - platform: state
    entity_id: binary_sensor.nvr_motion_1
    to: "on"
action:
  - service: light.turn_on
    target:
      entity_id: light.entrance
```

### 2. Motion → Notification with Snapshot
```yaml
alias: Motion Camera 3 Notification
trigger:
  - platform: state
    entity_id: binary_sensor.nvr_motion_3
    to: "on"
action:
  - service: notify.mobile_app_myphone
    data:
      message: "Motion detected on Camera 3!"
```

### 3. Motion → Camera Popup (Browser Mod)
(*requires [Browser Mod](https://github.com/thomasloven/hass-browser_mod)*)
```yaml
alias: Motion Camera 5 Notification with Snapshot Event
trigger:
  - platform: event
    event_type: provision_isr_events.snapshot_saved
    event_data:
      entity_id: binary_sensor.nvr_motion_5
action:
  - service: notify.mobile_app_myphone
    data:
      message: "Motion detected on Camera 5!"
      data:
        image: "{{ trigger.event.data.url }}"

```

## Troubleshooting
- If sensors remain OFF, ensure the ONVIF user in the NVR has permission for **Event service**.  
- Use Home Assistant **Developer Tools → States** to check sensor values.  
- Enable DEBUG logs in `configuration.yaml` if needed:  
```yaml
logger:
  default: warning
  logs:
    custom_components.provision_isr_events: debug
```

## Disclaimer
This is a **community-made integration**.  
- It is **not affiliated with, authorized, or endorsed by Provision-ISR**.  
- The Provision-ISR name and logo are trademarks of their respective owners.  
- This project does not attempt to misuse, exploit, or misrepresent the brand.  
- Use at your own risk.  

## License
MIT License  

Copyright (c) 2025 Fabio Bellavia

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:  

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.  

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
