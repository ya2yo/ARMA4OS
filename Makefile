.PHONY: check test demo build build-release help test-real-check test-real test-vm
.DEFAULT_GOAL := build

PYTHON ?= python3
SCX_SIMPLE ?= /root/sched-ext-dev/tools/sched_ext/build/bin/scx_simple
REAL_REPEATS ?= 5
REAL_SEED ?= 42
REAL_OUTPUT ?=
VM_HOST ?= root@192.168.66.128
VM_TRANSPORT ?= auto
VM_ROOT ?= /root/arma4os-vm-tests
VM_OUTPUT ?=
VM_TIMEOUT ?= 1200

help:
	@echo "make test / check / demo / build / build-release"
	@echo "make test-real-check  Check this host for real sched_ext testing (read only)"
	@echo "make test-real        Build and test default/scx_simple + rollback/EOF/SIGTERM"
	@echo "  Run on the openEuler guest with BPF privileges; no automatic sudo or SSH."
	@echo "  SCX_SIMPLE=/absolute/path REAL_REPEATS=5 REAL_SEED=42 REAL_OUTPUT=results/new-run"
	@echo "make test-vm          SSH -> sync source -> guest test-real -> download results"
	@echo "  VM_HOST=root@192.168.66.128 VM_TRANSPORT=auto|windows|native"
	@echo "  VM_ROOT=/root/arma4os-vm-tests VM_OUTPUT=results/new-vm-run"

test-vm:
	$(PYTHON) -m benchmarks.vm_smoke --host "$(VM_HOST)" --transport "$(VM_TRANSPORT)" \
	  --remote-root "$(VM_ROOT)" --scheduler "$(SCX_SIMPLE)" \
	  --repeats "$(REAL_REPEATS)" --seed "$(REAL_SEED)" --timeout "$(VM_TIMEOUT)" $(if $(VM_OUTPUT),--output "$(VM_OUTPUT)")

build:
	cargo build --workspace --locked

build-release:
	cargo build --workspace --release --locked

test-real-check:
	$(PYTHON) -m benchmarks.real_smoke --check-only --scheduler "$(SCX_SIMPLE)"

test-real:
	$(PYTHON) -m benchmarks.real_smoke --check-only --scheduler "$(SCX_SIMPLE)"
	$(MAKE) build-release
	$(PYTHON) -m benchmarks.real_smoke --scheduler "$(SCX_SIMPLE)" \
	  --executor ./target/release/arma-executor --repeats "$(REAL_REPEATS)" \
	  --seed "$(REAL_SEED)" $(if $(REAL_OUTPUT),--output "$(REAL_OUTPUT)")

test:
	python3 -m unittest discover -s tests -v
	cargo test --workspace --locked

check:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	python3 -m compileall -q arma4os benchmarks tests

demo:
	python3 -m arma4os demo
