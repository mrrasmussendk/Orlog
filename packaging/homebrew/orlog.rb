# typed: false
# frozen_string_literal: true

# Homebrew formula for orlog.
#
# This is the SOURCE of truth for the formula; the tap repo
# (mrrasmussendk/homebrew-tap) carries a copy at Formula/orlog.rb.
# `packaging/homebrew/bump.py` regenerates this file for a new release.
#
# It installs the prebuilt binary from GitHub Releases rather than building
# from source. Building from source under Homebrew would mean vendoring
# every transitive dependency as a `resource` block -- including
# onnxruntime and numpy, which ship as large platform-specific wheels --
# which is brittle to maintain and slow to install.
#
# Installing this way also sidesteps the Gatekeeper prompt that a browser
# download produces: Homebrew fetches over curl, which does not set the
# com.apple.quarantine attribute, so the ad-hoc-signed binary runs without
# needing to be notarized.
class Orlog < Formula
  desc "Memory for AI agents that would rather say \"I don't know\" than be wrong"
  homepage "https://github.com/mrrasmussendk/Orlog"
  version "0.0.3"
  license "Apache-2.0"

  on_macos do
    on_arm do
      url "https://github.com/mrrasmussendk/Orlog/releases/download/v0.0.3/orlog-0.0.3-macos-arm64.zip"
      sha256 "06c260f29e6bd62d2334925173bada8eb6abf790a0aa8a66750a4d54bbe1fb70"
    end

    # No Intel build: the release workflow's macOS runner is Apple Silicon,
    # so only an arm64 binary is published. Intel users should install from
    # PyPI instead (pipx install orlog).
    on_intel do
      odie "orlog has no Intel macOS binary -- install from PyPI instead: pipx install orlog"
    end
  end

  on_linux do
    on_intel do
      url "https://github.com/mrrasmussendk/Orlog/releases/download/v0.0.3/orlog-0.0.3-linux-x86_64.zip"
      sha256 "c98e0e5a46ba5087b4264fe24e3fe9aee267e5a54d2f4e797ad2e74b82a4b643"
    end

    on_arm do
      odie "orlog has no arm64 Linux binary -- install from PyPI instead: pipx install orlog"
    end
  end

  def install
    bin.install "orlog"
  end

  def caveats
    <<~EOS
      orlog stores PII under an encryption key it never writes to disk.
      `orlog init` prints one; export it before `orlog serve`:

        orlog init myworkspace
        export ORLOG_VAULT_KEY=...

      To register the workspace with Claude Code in one step instead:

        orlog init myworkspace --claude-code
    EOS
  end

  test do
    assert_match "usage: orlog", shell_output("#{bin}/orlog --help")

    # A real end-to-end check: scaffold a workspace and verify its (empty)
    # hash chain, which exercises the log, config and index code paths
    # rather than just proving the binary starts.
    output = shell_output("#{bin}/orlog init #{testpath}/ws")
    assert_match "Workspace initialized", output
    key = output[/ORLOG_VAULT_KEY=(\S+)/, 1]
    assert key, "init did not print a vault key"

    with_env(ORLOG_VAULT_KEY: key) do
      assert_match "hash chain verified", shell_output("#{bin}/orlog replay #{testpath}/ws")
    end
  end
end
