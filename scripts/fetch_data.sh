#!/usr/bin/env bash
# Downloads the Olist Brazilian E-Commerce Public Dataset (CC BY-NC-SA 4.0) from a public GitHub LFS mirror
# and verifies SHA-256 checksums. Original: kaggle.com/datasets/olistbr/brazilian-ecommerce
set -euo pipefail
cd "$(dirname "$0")/../data/raw"
B=https://media.githubusercontent.com/media/Mayur313/Code-Champions_001/main
for f in olist_order_payments_dataset olist_orders_dataset olist_order_items_dataset olist_sellers_dataset \
         olist_customers_dataset olist_order_reviews_dataset olist_products_dataset product_category_name_translation; do
  curl -sfL -o "$f.csv" "$B/$f.csv"
done
sha256sum -c SHA256SUMS
