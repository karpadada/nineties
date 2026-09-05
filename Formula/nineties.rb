class Nineties < Formula
  desc "Local YouTube Music browser and MP3 collection manager"
  homepage "https://github.com/karpadada/nineties"
  url "https://github.com/karpadada/nineties.git", tag: "v0.7.1"
  license "MIT"
  head "https://github.com/karpadada/nineties.git", branch: "main"

  depends_on "deno"
  depends_on "ffmpeg"
  depends_on "python@3.12"
  depends_on "uv"

  def install
    libexec.install "pyproject.toml", "uv.lock", "src"
    libexec.install "scripts/nineties", "scripts/prune-runtime-versions"

    runtime_path = [
      formula_opt_bin("uv"),
      formula_opt_bin("python@3.12"),
      formula_opt_bin("ffmpeg"),
      formula_opt_bin("deno"),
      "$PATH",
    ].join(":")
    (bin/"nineties").write_env_script libexec/"nineties",
                                        NINETIES_BREW_FORMULA: "karpadada/nineties/nineties",
                                        NINETIES_PACKAGE_ROOT: libexec,
                                        PATH:                  runtime_path
  end

  test do
    project_version = (libexec/"pyproject.toml").read[/^version = "([^"]+)"/, 1]
    assert_equal "nineties #{project_version}\n", shell_output("#{bin}/nineties --version")
    assert_match "nineties plugins install", shell_output("#{bin}/nineties --help")
  end
end
