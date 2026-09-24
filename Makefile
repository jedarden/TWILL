# TWILL install entry point (plan §13.1).
#
# `make install` puts the CLI on PATH and lays down the operator config
# skeleton.  It is idempotent and never overwrites operator state: an existing
# config file is kept, and a non-symlink binary in the way is a loud refusal,
# not a clobber.  The three systemd --user timers install from systemd/ and are
# wired into this target as their own beads land (each invokes verbs from a
# different phase, so each unit ships separately).

BIN_DIR ?= $(HOME)/.local/bin
CONFIG_DIR ?= $(HOME)/.config/twill
CONFIG_FILE := $(CONFIG_DIR)/config.toml
SKELETON := config.toml.skeleton
LINK := $(BIN_DIR)/twill

# Unit tests under the open-path audit gate (plan §8.3, §10.2).  Putting
# tests/ on PYTHONPATH makes `site` import tests/sitecustomize.py before
# unittest loads anything, so the hook is live from interpreter startup, and
# the inherited variable makes every spawned CLI verb self-install it.  A
# bare `python3 -m unittest discover -s tests` skips that startup and runs
# ungated, which tests/test_openpath.py detects and fails: run the suite
# through this target or pytest.
.PHONY: test
test:
	PYTHONPATH="$(CURDIR)/tests$${PYTHONPATH:+:$$PYTHONPATH}" \
		python3 -m unittest discover -s tests

.DEFAULT_GOAL := install
.PHONY: install
install:
	@mkdir -p "$(BIN_DIR)" "$(CONFIG_DIR)"
	@if [ -e "$(LINK)" ] && [ ! -L "$(LINK)" ]; then \
		echo "twill: error: $(LINK) exists and is not a symlink; move it aside first" >&2; \
		exit 1; \
	fi
	@ln -sfn "$(CURDIR)/twill" "$(LINK)"
	@if [ -e "$(CONFIG_FILE)" ]; then \
		echo "twill: keeping existing $(CONFIG_FILE)"; \
	else \
		install -m 600 "$(SKELETON)" "$(CONFIG_FILE)"; \
		echo "twill: installed config skeleton at $(CONFIG_FILE)"; \
		echo "twill: edit it to set artifacts_root (it has no default) before the first run"; \
	fi
	@echo "twill: installed $(LINK) -> $(CURDIR)/twill"
