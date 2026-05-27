#pragma once

// Copy this file to monitor_config.local.h and edit the values there.

#undef MONITOR_WIFI_SSID
#define MONITOR_WIFI_SSID "your-wifi-ssid"

#undef MONITOR_WIFI_PASSWORD
#define MONITOR_WIFI_PASSWORD "your-wifi-password"

#undef MONITOR_FIRESTORE_PROJECT_ID
#define MONITOR_FIRESTORE_PROJECT_ID "your-project-id"

#undef MONITOR_FIRESTORE_DOCUMENT_PATH
#define MONITOR_FIRESTORE_DOCUMENT_PATH "usage/current"

// Optional. Leave blank if your Firestore REST read works without an API key.
#undef MONITOR_FIRESTORE_API_KEY
#define MONITOR_FIRESTORE_API_KEY "your-firebase-web-api-key"

// Optional: override Codex Firestore document path (default: usage/codex)
// #undef MONITOR_CODEX_DOCUMENT_PATH
// #define MONITOR_CODEX_DOCUMENT_PATH "usage/codex"

#undef MONITOR_DASHBOARD_TITLE
#define MONITOR_DASHBOARD_TITLE "Usage Monitor"

#undef MONITOR_DISPLAY_ROTATION
#define MONITOR_DISPLAY_ROTATION 1
