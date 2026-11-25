-- watch-history.lua
-- Appends one JSONL record per played file to ~/.config/mpv/watch-history.jsonl.
-- Nothing touches the disk until a file closes. No playback behavior is changed.
-- Fields: filename, size_bytes, opened, closed, percent, watched_secs, folder

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

-- Name of the folder that contains the file (not the full path). nil for URLs.
local function parent_folder()
    local p = mp.get_property("path") or ""
    if p == "" or p:find("://", 1, true) then return nil end
    if not p:find("^/") then p = utils.join_path(utils.getcwd() or "", p) end
    local dir = utils.split_path(p)
    local name = dir and dir:match("([^/]+)/*$")
    if name == "." or name == ".." then return nil end
    return name
end

-- Real playing time: counts only while the player is actually running
-- (not paused, not buffering). Only reacts to pause/unpause, no polling.
local function tick(idle)
    if not current then return end
    local now = mp.get_time()  -- monotonic seconds
    if current.since then
        current.watched = current.watched + (now - current.since)
        current.since = nil
    end
    if not idle then current.since = now end
end

-- Write the current record (if any) and clear it. Safe to call repeatedly.
local function close_record()
    local rec = current
    if not rec then return end
    tick(true)        -- stop the clock
    current = nil     -- clear first so a record can never be written twice

    local f, err = io.open(LOG_PATH, "a")
    if not f then
        mp.msg.warn("watch-history: cannot open log: " .. tostring(err))
        return
    end

    local name   = utils.format_json(rec.name) or '""'  -- handles JSON escaping
    local folder = rec.folder and utils.format_json(rec.folder) or "null"
    local size   = rec.size and string.format("%.0f", rec.size) or "null"
    local pct    = rec.percent and string.format("%.0f", rec.percent) or "null"
    f:write(string.format(
        '{"filename":%s,"size_bytes":%s,"opened":"%s","closed":"%s","percent":%s,"watched_secs":%.0f,"folder":%s}\n',
        name, size, rec.opened, timestamp(), pct, rec.watched, folder))
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
        name    = mp.get_property("filename") or "unknown",  -- no directory part
        folder  = parent_folder(),
        size    = size,                                      -- nil -> null
        opened  = timestamp(),
        percent = nil,                                       -- filled in on unload
        watched = 0,
        since   = nil,
    }
    tick(mp.get_property_bool("core-idle", true))
end)

-- core-idle is true while paused or buffering, false while playing.
mp.observe_property("core-idle", "bool", function(_, idle)
    if current and idle ~= nil then tick(idle) end
end)

-- Runs just before the file is unloaded, while the playback position is still
-- readable. Cached in memory only; the disk is not touched here.
mp.add_hook("on_unload", 50, function()
    if current then
        tick(true)
        local p = mp.get_property_number("percent-pos")
        if p then current.percent = math.max(0, math.min(100, p)) end
    end
end)

-- Fires for every way a file can end (eof, stop, quit, error, redirect).
-- We key off "did this file actually load?" (current ~= nil), not off the reason.
-- Reaching the natural end of the file counts as 100%.
mp.register_event("end-file", function(ev)
    if current and ev and ev.reason == "eof" then current.percent = 100 end
    close_record()
end)

-- Fallback for shutdown; a no-op if end-file already wrote the record.
mp.register_event("shutdown", function() close_record() end)
