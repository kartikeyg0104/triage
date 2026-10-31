#!/bin/sh
echo "expect: Segmentation fault in parse_url"
urlfuzz --target parse_url --length 100000
