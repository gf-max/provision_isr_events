# Provision-ISR ONVIF Events & Camera Integration for Home Assistant

This is a **custom integration for Home Assistant** that provides support for **Provision-ISR NVRs and cameras** using the ONVIF protocol.

## ✨ Features

- **Event handling**  
  - Subscribes to ONVIF event streams (motion, alarms, channels).  
  - Creates `binary_sensor` entities in Home Assistant for each active channel.  

- **Camera support**  
  - Discovers ONVIF media profiles (via `GetProfiles`).  
  - Resolves RTSP stream URIs (`GetStreamUri`).  
  - Provides still images (snapshots) with full **Digest authentication** support and vendor-specific fallbacks.  
  - Creates one `camera` entity per channel/profile.  

- **Vendor fallbacks**  
  - Works with Provision-ISR native snapshot endpoints.  
  - Compatible with Dahua/Hikvision OEM snapshot URLs when channel mapping is known.  

- **Config Flow**  
  - Integration is configured directly from the UI (no YAML needed).  
  - Supports options for auto-off timers, channel selection, snapshot timeouts, etc.

## 📦 Installation

### HACS (recommended)
1. In HACS, add this repository as a **Custom Repository** (type: Integration).  
2. Search for **Provision-ISR Events** and install.  
3. Restart Home Assistant.

### Manual
1. Copy the `custom_components/provision_isr_events/` folder into your Home Assistant `config/custom_components/`.  
2. Restart Home Assistant.

## ⚙️ Configuration

1. Go to **Settings → Devices & Services → Add Integration**.  
2. Search for **Provision-ISR ONVIF Events**.  
3. Enter the connection details of your NVR (host, port, username, password).  
4. After setup, sensors and camera entities will be created automatically.

### Options
- **Auto OFF (seconds)**: automatically reset motion sensors after N seconds.  
- **Channels**: restrict discovery to specific channels (e.g. `1,2,6`).  
- **Snapshot mode**: choose between `auto`, `urlcreds`, `basic`, or disable snapshots.  
- **Snapshot timeout**: maximum wait time for a snapshot request.

## 🖼️ Branding

This integration includes a **generic CCTV icon set** to avoid trademark issues.  
Logos and icons are published under [home-assistant/brands](https://github.com/home-assistant/brands).

## 🔒 Disclaimer

This project is **community-maintained** and **not affiliated with Provision-ISR**.  
Use at your own risk. Tested on NVR5-8200PX+ and similar Provision-ISR devices.

## 📜 License

Released under the MIT License. See [LICENSE](LICENSE) for details.
