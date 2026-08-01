.PHONY: test clean

test:
	python3 -m unittest discover -s tests -v

clean:
	rm -rf __pycache__ tests/__pycache__
