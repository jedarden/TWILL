# TWILL install entry point (plan §13.1).
#
# `make install` puts the CLI on PATH and lays down the operator config
# skeleton and the ingest, measure, and digest timers.  It is idempotent and never
# overwrites operator state: an existing config file is kept, and a
# non-symlink binary in the way is a loud refusal, not a clobber.

BIN_DIR ?= $(HOME)/.local/bin
CONFIG_DIR ?= $(HOME)/.config/twill
CONFIG_FILE := $(CONFIG_DIR)/config.toml
SKELETON := config.toml.skeleton
LINK := $(BIN_DIR)/twill
LEARNING_LINK := $(BIN_DIR)/twill-learning
ARCHIVE_FEED_LINK := $(BIN_DIR)/twill-archive-feed
SYSTEMD_USER_DIR ?= $(HOME)/.config/systemd/user
SYSTEMD_UNITS := twill-ingest.service twill-ingest.timer twill-measure.service twill-measure.timer twill-digest.service twill-digest.timer twill-learning.service twill-learning.timer twill-discover.service twill-discover.timer twill-archive-feed.service twill-archive-feed.timer

# Unit tests under the import, network, and open-path audit gates (plan §3,
# §8.3, §10.2).  Putting
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
	@if [ -e "$(ARCHIVE_FEED_LINK)" ] && [ ! -L "$(ARCHIVE_FEED_LINK)" ]; then \
		echo "twill: error: $(ARCHIVE_FEED_LINK) is not a symlink" >&2; exit 1; \
	fi
	@if [ -e "$(LEARNING_LINK)" ] && [ ! -L "$(LEARNING_LINK)" ]; then \
		echo "twill: error: $(LEARNING_LINK) is not a symlink" >&2; exit 1; \
	fi
	@if [ -e "$(LINK)" ] && [ ! -L "$(LINK)" ]; then \
		echo "twill: error: $(LINK) exists and is not a symlink; move it aside first" >&2; \
		exit 1; \
	fi
	@ln -sfn "$(CURDIR)/twill" "$(LINK)"
	@ln -sfn "$(CURDIR)/twill-learning" "$(LEARNING_LINK)"
	@ln -sfn "$(CURDIR)/scripts/export-archive-transcripts.py" "$(ARCHIVE_FEED_LINK)"
	@if [ ! -e "$(CONFIG_DIR)/learning.json" ]; then \
		install -m 600 learning.policy.example.json "$(CONFIG_DIR)/learning.json"; \
	fi
	@if [ ! -e "$(CONFIG_DIR)/archive-feed.json" ]; then \
		install -m 600 archive-feed.policy.example.json "$(CONFIG_DIR)/archive-feed.json"; \
	fi
	@if [ -e "$(CONFIG_FILE)" ]; then \
		echo "twill: keeping existing $(CONFIG_FILE)"; \
	else \
		install -m 600 "$(SKELETON)" "$(CONFIG_FILE)"; \
		echo "twill: installed config skeleton at $(CONFIG_FILE)"; \
		echo "twill: edit it to set artifacts_root (it has no default) before the first run"; \
	fi
	@mkdir -p "$(SYSTEMD_USER_DIR)"
	@for unit in $(SYSTEMD_UNITS); do \
		install -m 644 "$(CURDIR)/systemd/$$unit" "$(SYSTEMD_USER_DIR)/$$unit"; \
	done
	@echo "twill: installed $(LINK) -> $(CURDIR)/twill"
	@echo "twill: installed ingest, measure, and digest units in $(SYSTEMD_USER_DIR)"
