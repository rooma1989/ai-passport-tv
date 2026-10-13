// TEST STUB ONLY -- see av_soundprov.h in this directory. Never decodes
// anything; it only proves the integration compiles and links.
#include "av_soundprov.h"

#include <stdlib.h>
#include <string.h>

struct av_soundprov { size_t samples; };

av_soundprov_t *av_soundprov_create(void) { return calloc(1, sizeof(av_soundprov_t)); }
void av_soundprov_destroy(av_soundprov_t *d) { free(d); }
void av_soundprov_reset(av_soundprov_t *d) { if (d) d->samples = 0; }
int av_soundprov_feed(av_soundprov_t *d, const int16_t *pcm, size_t n)
{
    (void)pcm;
    if (d) d->samples += n;
    return 0;
}
int av_soundprov_progress(const av_soundprov_t *d) { (void)d; return 0; }
int av_soundprov_result(const av_soundprov_t *d, av_soundprov_result_t *out)
{
    (void)d;
    if (out) memset(out, 0, sizeof(*out));
    return -1;
}
