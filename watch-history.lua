-- watch-history.lua
-- Appends one JSONL record per played file to ~/.config/mpv/watch-history.jsonl.
-- Nothing touches the disk until a file closes. No playback behavior is changed.

local utils = require "mp.utils"

-- "~~/" is mpv's config directory (~/.config/mpv by default)
local LOG_PATH = mp.command_native({"expand-path", "~~/watch-history.jsonl"})

-- Record for the file currently playing. Lives in memory only.
local current = nil

-- Local time with UTC offset, e.g. 2026-11-24T20:03:11+01:00
local function timestamp()
    local s = os.date("%Y-%m-%dT%H:%M:%S%z")
    return (s:gsub("(%d%d)(%d%d)$", "%1:%2"))
end

-- Write the current record (if any) and clear it. Safe to call repeatedly.
local function close_record()
    local rec = current
    if not rec then return end
    current = nil  -- clear first so a record can never be written twice

    local f, err = io.open(LOG_PATH, "a")
    if not f then
        mp.msg.warn("watch-history: cannot open log: " .. tostring(err))
        return
    end

    local name = utils.format_json(rec.name) or '""'  -- handles JSON escaping
    local size = rec.size and string.format("%.0f", rec.size) or "null"
    f:write(string.format(
        '{"filename":%s,"size_bytes":%s,"opened":"%s","closed":"%s"}\n',
        name, size, rec.opened, timestamp()))
    f:close()
end

-- Fires only once a file has actually opened (never for missing/unplayable files).
mp.register_event("file-loaded", function()
    close_record()  -- safety net: never leave a previous record dangling

    local size = mp.get_property_number("file-size")
    if not size then
        local info = utils.file_info(mp.get_property("path") or "")
        size = info and info.size
    end

    current = {
        name   = mp.get_property("filename") or "unknown",  -- no directory part
        size   = size,                                      -- nil -> null
        opened = timestamp(),
    }
end)

-- Fires for every way a file can end (eof, stop, quit, error, redirect).
-- We key off "did this file actually load?" (current ~= nil), not off the reason.
mp.register_event("end-file", close_record)

-- Fallback for shutdown; a no-op if end-file already wrote the record.
mp.register_event("shutdown", close_record)
