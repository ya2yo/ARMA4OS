.PHONY: check test demo build

build:
	cargo build --workspace --locked

test:
	python3 -m unittest discover -s tests -v
	cargo test --workspace --locked

check:
	cargo fmt --all -- --check
	cargo clippy --workspace --all-targets --locked -- -D warnings
	python3 -m compileall -q arma4os benchmarks tests

demo:
	python3 -m arma4os demo
