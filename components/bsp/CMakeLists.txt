set(srcs "src/bsp_display.c" "src/bsp_display_lvgl.c")
set(priv_includes)
if(CONFIG_AV_BOARD_XINGZHI_CUBE_154)
    list(APPEND srcs
        "src/xingzhi_cube_154/bsp_audio.c"
        "src/xingzhi_cube_154/bsp_battery.c"
        "src/xingzhi_cube_154/bsp_button.c"
        "src/xingzhi_cube_154/bsp_i2c.c"
        "src/xingzhi_cube_154/bsp_power.c"
        "src/xingzhi_cube_154/bsp_xz_math.c")
    list(APPEND priv_includes "src/xingzhi_cube_154")
else()
    list(APPEND srcs
        "src/passport/bsp_audio.c"
        "src/passport/bsp_battery.c"
        "src/passport/bsp_button.c"
        "src/passport/bsp_i2c.c"
        "src/passport/bsp_power.c")
endif()

idf_component_register(
    SRCS ${srcs}
    INCLUDE_DIRS "include"
    PRIV_INCLUDE_DIRS ${priv_includes}
    REQUIRES driver esp_driver_i2c esp_driver_i2s esp_lcd esp_lvgl_port button esp_adc esp_codec_dev
)
