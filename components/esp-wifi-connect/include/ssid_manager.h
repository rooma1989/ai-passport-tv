#ifndef SSID_MANAGER_H
#define SSID_MANAGER_H

#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

struct SsidItem {
    std::string ssid;
    std::string password;
    // 0 = unknown. 1-14 = 2.4 GHz, 36-177 = 5 GHz. The ranges do not overlap,
    // so one uint8_t NVS key (channel / channelN) covers both bands.
    uint8_t channel = 0;
};

class SsidManager {
public:
    static SsidManager& GetInstance() {
        static SsidManager instance;
        return instance;
    }

    // How many networks are kept. AddSsid() on a full list drops the last one;
    // the phone remote checks IsFull() first and asks the viewer instead.
    static constexpr size_t kMaxCount = 10;

    void AddSsid(const std::string& ssid, const std::string& password, uint8_t channel = 0);
    void UpdateSsidChannel(const std::string& ssid, uint8_t channel);
    void RemoveSsid(int index);
    void SetDefaultSsid(int index);
    // One place up (index 1 becomes 0, and so on); the order is the order in
    // which visible networks are tried (wifi_order.h).
    void MoveUp(int index);
    void Clear();
    bool IsFull() const;
    // A copy, taken under the lock: the list is changed from the phone remote's
    // worker while the Wi-Fi event task reads it for a scan.
    std::vector<SsidItem> GetSsidList() const;
    std::vector<uint8_t> GetSavedChannels() const;

private:
    SsidManager();
    ~SsidManager();

    void LoadFromNvs();
    void SaveToNvs();

    std::vector<SsidItem> ssid_list_;
    mutable std::recursive_mutex mutex_;
};

#endif // SSID_MANAGER_H
