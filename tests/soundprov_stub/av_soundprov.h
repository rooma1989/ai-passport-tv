// TEST STUB ONLY -- not part of the firmware.
//
// The sound-provisioning decoder is built on its own branch (main/av_soundprov.c
// and .h, branch sound-prov). This copy of its agreed interface lets the
// integration in main/av_soundsetup.c be compiled and linked with the decoder
// path switched on before the two branches are merged: copy both files of this
// directory into main/ for a throwaway build, then delete them again. Never
// commit them under main/ -- the real module replaces them.
#pragma once
#include <stddef.h>
#include <stdint.h>

typedef struct av_soundprov av_soundprov_t;
av_soundprov_t *av_soundprov_create(void);
void av_soundprov_destroy(av_soundprov_t *d);
void av_soundprov_reset(av_soundprov_t *d);
int av_soundprov_feed(av_soundprov_t *d, const int16_t *pcm, size_t n);   // 16 kHz mono; 1 when a message decoded
int av_soundprov_progress(const av_soundprov_t *d);                       // 0..100
typedef struct { char ssid[33]; char password[65]; char server[64]; } av_soundprov_result_t;
int av_soundprov_result(const av_soundprov_t *d, av_soundprov_result_t *out);
