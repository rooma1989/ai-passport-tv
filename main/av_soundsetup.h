// Sound provisioning on the setup screen: the microphone feeds the decoder
// (av_soundprov.h, a separate module), and a decoded network is tried and
// saved exactly as the hotspot page's form would (av_provision.h).
//
// While setup runs, the phone page <site>/tv/wifi plays the network's name and
// password as sound; the viewer holds the phone near the device and presses
// "send". The hotspot keeps running beside it as the fallback.
//
// Compiled to "not available" when av_soundprov.h is not in the tree, so this
// branch builds on its own; with the decoder merged in it turns on by itself.
// A task captures 16 kHz mono and runs the decoder; it exists only between
// av_soundsetup_start() and av_soundsetup_stop(), and the decoder's memory with
// it. On the AI Passport the codec must already be open at 16 kHz/16 bit/mono
// (the setup screen opens it for its hiss); on the Xingzhi the microphone has
// its own I2S port.
#pragma once
#include <stdbool.h>
#include <stddef.h>

#include "av_provision_policy.h"

// Whether this build has the decoder.
bool av_soundsetup_available(void);

// Start listening. False when unavailable or the microphone or memory is not
// there; setup carries on without it.
bool av_soundsetup_start(void);

// Stop listening and free everything. Waits for the task to finish (at most a
// connection attempt in progress, ~25 s; normally one read, 20 ms).
void av_soundsetup_stop(void);

// What the setup screen shows: the state and, while receiving, 0..100.
av_sound_state_t av_soundsetup_state(unsigned *progress);
