// Managing saved networks from the phone: what each message is allowed to do
// to the list, the names the phone sees, and the ack JSON (main/av_wifi_ops.c).
#include "av_wifi_ops.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static av_wifi_op_t op(av_wifi_op_kind_t kind, uint64_t seq, int index, const char *ssid, const char *pw)
{
    av_wifi_op_t o;
    memset(&o, 0, sizeof(o));
    o.kind = kind;
    o.seq = seq;
    o.index = index;
    snprintf(o.ssid, sizeof(o.ssid), "%s", ssid ? ssid : "");
    snprintf(o.password, sizeof(o.password), "%s", pw ? pw : "");
    return o;
}

static void test_plan(void)
{
    const char *names[] = { "home", "office", ("\xff" "bad") };
    av_wifi_plan_t plan;
    av_wifi_op_t o;

    o = op(AV_WIFI_OP_ADD, 10, 0, "cafe", "12345678");
    assert(av_wifi_plan(&o, 9, names, 3, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_ADD);
    // Replays and a zero seq.
    assert(av_wifi_plan(&o, 10, names, 3, &plan) == AV_WIFI_E_REPLAY);
    assert(av_wifi_plan(&o, 11, names, 3, &plan) == AV_WIFI_E_REPLAY);
    o.seq = 0;
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_BAD);
    // Passwords: open, too short, WPA range, 64 hex.
    o = op(AV_WIFI_OP_ADD, 20, 0, "cafe", "");
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK);
    o = op(AV_WIFI_OP_ADD, 20, 0, "cafe", "1234567");
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_BAD);
    o = op(AV_WIFI_OP_ADD, 20, 0, "", "12345678");
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_BAD);
    o = op(AV_WIFI_OP_ADD, 20, 0, "cafe", "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef");
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK);

    // Full: a new network is refused, a known one only changes its password.
    const char *full[AV_WIFI_SAVED_MAX] = { "a", "b", "c", "d", "e", "f", "g", "h", "i", "j" };
    o = op(AV_WIFI_OP_ADD, 30, 0, "k", "12345678");
    assert(av_wifi_plan(&o, 0, full, AV_WIFI_SAVED_MAX, &plan) == AV_WIFI_E_FULL);
    o = op(AV_WIFI_OP_ADD, 30, 0, "e", "12345678");
    assert(av_wifi_plan(&o, 0, full, AV_WIFI_SAVED_MAX, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_ADD);

    // Index and name must agree with the list.
    o = op(AV_WIFI_OP_REMOVE, 40, 1, "office", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_REMOVE && plan.index == 1);
    o = op(AV_WIFI_OP_REMOVE, 40, 1, "home", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_CHANGED);
    o = op(AV_WIFI_OP_REMOVE, 40, 3, "home", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_CHANGED);
    o = op(AV_WIFI_OP_REMOVE, 40, -1, "home", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_CHANGED);
    // A name that is not text is matched by the name the phone was shown.
    o = op(AV_WIFI_OP_SWITCH, 41, 2, "?bad", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_SWITCH && plan.index == 2);
    // Up: the first one stays (nothing to do), the others move.
    o = op(AV_WIFI_OP_UP, 42, 0, "home", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_NONE);
    o = op(AV_WIFI_OP_UP, 42, 1, "office", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_OK && plan.action == AV_WIFI_OP_UP && plan.index == 1);
    // Unknown kinds and an empty list.
    o = op(AV_WIFI_OP_NONE, 43, 0, "home", NULL);
    assert(av_wifi_plan(&o, 0, names, 3, &plan) == AV_WIFI_E_BAD);
    o = op(AV_WIFI_OP_SWITCH, 44, 0, "home", NULL);
    assert(av_wifi_plan(&o, 0, NULL, 0, &plan) == AV_WIFI_E_CHANGED);

    assert(av_wifi_op_kind("add") == AV_WIFI_OP_ADD && av_wifi_op_kind("switch") == AV_WIFI_OP_SWITCH);
    assert(av_wifi_op_kind("up") == AV_WIFI_OP_UP && av_wifi_op_kind("remove") == AV_WIFI_OP_REMOVE);
    assert(av_wifi_op_kind("list") == AV_WIFI_OP_NONE && av_wifi_op_kind(NULL) == AV_WIFI_OP_NONE);
    assert(!strcmp(av_wifi_err_code(AV_WIFI_OK), "ok") && !strcmp(av_wifi_err_code(AV_WIFI_E_FULL), "full"));
    assert(!strcmp(av_wifi_err_code((av_wifi_err_t)99), "bad"));
}

static void test_display(void)
{
    char out[100];
    av_wifi_display_name("家里的WiFi", out, sizeof(out));
    assert(!strcmp(out, "家里的WiFi"));
    av_wifi_display_name("a\x01" "b\x7f", out, sizeof(out));
    assert(!strcmp(out, "a?b?"));
    // Bad lead byte, truncated sequence, overlong form, surrogate.
    av_wifi_display_name("\xc3", out, sizeof(out));
    assert(!strcmp(out, "?"));
    av_wifi_display_name("\xe5\xae", out, sizeof(out));
    assert(!strcmp(out, "??"));
    av_wifi_display_name("\xc0\xaf", out, sizeof(out));
    assert(!strcmp(out, "??"));
    av_wifi_display_name("\xed\xa0\x80", out, sizeof(out));
    assert(!strcmp(out, "???"));
    // Never cuts a character in half.
    av_wifi_display_name("ab家", out, 5);
    assert(!strcmp(out, "ab"));
    av_wifi_display_name(NULL, out, sizeof(out));
    assert(!strcmp(out, ""));
}

static void test_json(void)
{
    char out[600];
    assert(av_wifi_json_string("a\"b\\c\n", out, sizeof(out)) == strlen("\"a\\\"b\\\\c\\u000a\""));
    assert(!strcmp(out, "\"a\\\"b\\\\c\\u000a\""));
    assert(av_wifi_json_string("abc", out, 5) == 0);
    assert(av_wifi_json_string("abc", out, 6) == 5);

    const char *names[] = { "home", "o\"ffice", "\xff" };
    size_t n = av_wifi_ack_json(out, sizeof(out), 1760000000123ull, AV_WIFI_OK, names, 3, "home");
    assert(n == strlen(out));
    assert(!strcmp(out, "{\"wifi\":{\"ack\":1760000000123,\"ok\":1,\"err\":\"ok\",\"saved\":[\"home\",\"o\\\"ffice\",\"?\"],"
                        "\"cur\":\"home\"}}"));
    n = av_wifi_ack_json(out, sizeof(out), 5, AV_WIFI_E_CHANGED, NULL, 0, "");
    assert(!strcmp(out, "{\"wifi\":{\"ack\":5,\"ok\":0,\"err\":\"changed\",\"saved\":[],\"cur\":\"\"}}"));
    // Every size short of the whole answer is refused, never truncated.
    char exact[600];
    const size_t whole = av_wifi_ack_json(exact, sizeof(exact), 7, AV_WIFI_OK, names, 3, "x");
    for (size_t size = 1; size <= whole; size++) assert(av_wifi_ack_json(out, size, 7, AV_WIFI_OK, names, 3, "x") == 0);
    assert(av_wifi_ack_json(out, whole + 1, 7, AV_WIFI_OK, names, 3, "x") == whole);
    // Ten 32-byte names all escaped as \u00XX would not be text; ten long CJK names fit the STATUS budget.
    const char *wide[AV_WIFI_SAVED_MAX];
    for (unsigned i = 0; i < AV_WIFI_SAVED_MAX; i++) wide[i] = "家家家家家家家家家家";
    char big[1024];
    assert(av_wifi_ack_json(big, sizeof(big), UINT64_MAX, AV_WIFI_E_KEY, wide, AV_WIFI_SAVED_MAX, wide[0]) > 0);
}

int main(void)
{
    test_plan();
    test_display();
    test_json();
    puts("test_av_wifi_ops: PASS");
    return 0;
}
