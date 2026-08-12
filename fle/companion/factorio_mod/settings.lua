data:extend({
    {
        type = "int-setting",
        name = "airi-companion-bridge-port",
        setting_type = "runtime-global",
        default_value = 31501,
        minimum_value = 1024,
        maximum_value = 65535,
        order = "a"
    },
    {
        type = "string-setting",
        name = "airi-companion-session-token",
        setting_type = "runtime-global",
        default_value = "",
        allow_blank = true,
        order = "b"
    },
    {
        type = "bool-setting",
        name = "airi-companion-auto-spawn",
        setting_type = "runtime-global",
        default_value = true,
        order = "c"
    },
    {
        type = "string-setting",
        name = "airi-companion-display-name",
        setting_type = "runtime-global",
        default_value = "团子",
        allow_blank = false,
        order = "d"
    },
    {
        type = "int-setting",
        name = "airi-companion-respawn-seconds",
        setting_type = "runtime-global",
        default_value = 10,
        minimum_value = 1,
        maximum_value = 600,
        order = "e"
    },
    {
        type = "int-setting",
        name = "airi-companion-perception-radius",
        setting_type = "runtime-global",
        default_value = 64,
        minimum_value = 8,
        maximum_value = 256,
        order = "f"
    },
    {
        type = "double-setting",
        name = "airi-companion-follow-distance",
        setting_type = "runtime-global",
        default_value = 3,
        minimum_value = 1.5,
        maximum_value = 12,
        order = "g"
    }
})
