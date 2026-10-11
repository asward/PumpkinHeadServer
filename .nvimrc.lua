-- .nvimrc.lua — Arduino workspace config for asward/workspace-config.nvim
-- Requires: arduino-cli on PATH, board core installed (e.g. `arduino-cli core install arduino:avr`).
-- Build/upload from the shell: arduino-cli compile/upload --fqbn arduino:avr:uno -p /dev/ttyUSB0
--
-- LSP: arduino-language-server handles EVERY file in the sketch (.ino, .cpp, .c, .h), the same
-- way Arduino IDE 2 does. It runs clangd internally against its own preprocessed build.
--
-- Why not plain clangd for .cpp/.h? arduino-language-server can only translate locations in
-- files that were opened *through it*. A go-to-definition from the .ino that lands in, e.g.,
-- motor.cpp otherwise fails with "Document is not available" and the server exits. So on first
-- attach we also load every sketch source into a hidden buffer, so all of them are tracked.
-- Files added to the sketch later: run :LspRestart.

local FQBN = "arduino:avr:uno" -- `arduino-cli board listall` to find yours

local is_mac = vim.fn.has("mac") == 1
local cli_config = vim.fn.expand(is_mac and "~/Library/Arduino15/arduino-cli.yaml" or "~/.arduino15/arduino-cli.yaml")

-- Only attach inside a sketch folder (a directory containing a *.ino)
local function sketch_root(bufnr, on_dir)
    local root = vim.fs.root(bufnr, function(name)
        return name:match("%.ino$") ~= nil
    end)
    if root then
        on_dir(root)
    end
end

-- Load all sketch sources as hidden buffers so the server tracks them (see header comment)
local function track_sketch_files(client)
    if client._sketch_tracked then
        return
    end
    client._sketch_tracked = true
    local shm = vim.o.shortmess
    vim.o.shortmess = shm .. "A" -- don't prompt about swap files of files open elsewhere
    for name, kind in vim.fs.dir(client.root_dir) do
        if kind == "file" and (name:match("%.ino$") or name:match("%.[ch]$") or name:match("%.[ch]pp$")) then
            local b = vim.fn.bufadd(client.root_dir .. "/" .. name)
            if not vim.api.nvim_buf_is_loaded(b) then
                vim.bo[b].buflisted = false -- becomes listed when you actually open it
                pcall(vim.fn.bufload, b)
            end
        end
    end
    vim.o.shortmess = shm
end

return {
    -- Mason package names. clangd is not configured as its own server; arduino-language-server spawns it.
    lsp_servers = { "arduino-language-server", "clangd" },

    -- Keys are nvim-lspconfig server names; passed to vim.lsp.config()
    lsp_configs = {
        arduino_language_server = {
            filetypes = { "arduino", "c", "cpp" },
            root_dir = sketch_root,
            cmd = {
                "arduino-language-server",
                "-cli-config",
                cli_config, -- need not exist; arduino-cli falls back to defaults
                "-fqbn",
                FQBN,
                "-cli",
                "arduino-cli",
                "-clangd",
                "clangd",
            },
            on_attach = track_sketch_files,
            -- Work around arduino-language-server not supporting semantic tokens properly
            capabilities = {
                textDocument = { semanticTokens = vim.NIL },
                workspace = { semanticTokens = vim.NIL },
            },
        },
    },

    filetype_configs = {
        arduino = {
            opts = { tabstop = 2, shiftwidth = 2, expandtab = true },
        },
        cpp = {
            opts = { tabstop = 2, shiftwidth = 2, expandtab = true },
        },
        c = {
            opts = { tabstop = 2, shiftwidth = 2, expandtab = true },
        },
    },
}
