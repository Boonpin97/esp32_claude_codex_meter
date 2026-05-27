#pragma once

// Default monitor configuration. Override values in monitor_config.local.h.

#ifndef MONITOR_WIFI_SSID
#define MONITOR_WIFI_SSID ""
#endif

#ifndef MONITOR_WIFI_PASSWORD
#define MONITOR_WIFI_PASSWORD ""
#endif

#ifndef MONITOR_FIRESTORE_PROJECT_ID
#define MONITOR_FIRESTORE_PROJECT_ID "your-project-id"
#endif

#ifndef MONITOR_FIRESTORE_DOCUMENT_PATH
#define MONITOR_FIRESTORE_DOCUMENT_PATH "usage/current"
#endif

#ifndef MONITOR_CODEX_DOCUMENT_PATH
#define MONITOR_CODEX_DOCUMENT_PATH "usage/codex"
#endif

#ifndef MONITOR_FIRESTORE_API_KEY
#define MONITOR_FIRESTORE_API_KEY ""
#endif

#ifndef MONITOR_DASHBOARD_TITLE
#define MONITOR_DASHBOARD_TITLE "Claude Usage"
#endif

#ifndef MONITOR_DISPLAY_ROTATION
#define MONITOR_DISPLAY_ROTATION 1
#endif

#ifndef MONITOR_DATA_STALE_AFTER_MS
#define MONITOR_DATA_STALE_AFTER_MS 180000UL
#endif

#ifndef MONITOR_FIRESTORE_POLL_INTERVAL_MS
#define MONITOR_FIRESTORE_POLL_INTERVAL_MS 30000UL
#endif

#ifndef MONITOR_HTTP_TIMEOUT_MS
#define MONITOR_HTTP_TIMEOUT_MS 10000UL
#endif

#ifndef MONITOR_WIFI_CONNECT_TIMEOUT_MS
#define MONITOR_WIFI_CONNECT_TIMEOUT_MS 20000UL
#endif

#ifndef MONITOR_WIFI_RETRY_INTERVAL_MS
#define MONITOR_WIFI_RETRY_INTERVAL_MS 12000UL
#endif

#if __has_include("monitor_config.local.h")
#include "monitor_config.local.h"
#endif

namespace monitor_config
{

    inline constexpr const char *kWifiSsid = MONITOR_WIFI_SSID;
    inline constexpr const char *kWifiPassword = MONITOR_WIFI_PASSWORD;
    inline constexpr const char *kFirestoreProjectId = MONITOR_FIRESTORE_PROJECT_ID;
    inline constexpr const char *kFirestoreDocumentPath = MONITOR_FIRESTORE_DOCUMENT_PATH;
    inline constexpr const char *kCodexDocumentPath = MONITOR_CODEX_DOCUMENT_PATH;
    inline constexpr const char *kFirestoreApiKey = MONITOR_FIRESTORE_API_KEY;
    inline constexpr const char *kDashboardTitle = MONITOR_DASHBOARD_TITLE;
    inline constexpr int kDisplayRotation = MONITOR_DISPLAY_ROTATION;
    inline constexpr unsigned long kDataStaleAfterMs = MONITOR_DATA_STALE_AFTER_MS;
    inline constexpr unsigned long kFirestorePollIntervalMs = MONITOR_FIRESTORE_POLL_INTERVAL_MS;
    inline constexpr unsigned long kHttpTimeoutMs = MONITOR_HTTP_TIMEOUT_MS;
    inline constexpr unsigned long kWifiConnectTimeoutMs = MONITOR_WIFI_CONNECT_TIMEOUT_MS;
    inline constexpr unsigned long kWifiRetryIntervalMs = MONITOR_WIFI_RETRY_INTERVAL_MS;

} // namespace monitor_config
