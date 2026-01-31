#!/usr/bin/env python3
"""Export historic borrow data for specific symbols from parquet cache."""

import argparse
from io import BytesIO

import boto3
import pandas as pd


def export_symbols(bucket, market, symbols, output_file):
    """Export historic data for symbols from parquet files."""

    s3 = boto3.client('s3')

    # Get all parquet files for market
    prefix = f'ibkr/parquet/{market}/'
    response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    files = sorted([obj['Key'] for obj in response.get('Contents', [])
                   if obj['Key'].endswith('.parquet')])

    print(f'Found {len(files)} {market} parquet files')

    # Read all parquets and combine
    all_data = []

    for file_key in files:
        date = file_key.split('/')[-1].replace('.parquet', '')
        print(f'Reading {date}...')

        response = s3.get_object(Bucket=bucket, Key=file_key)
        df = pd.read_parquet(BytesIO(response['Body'].read()))

        # Filter for requested symbols
        symbol_data = df[df['symbol'].isin(symbols)]
        if not symbol_data.empty:
            all_data.append(symbol_data)

    # Combine all data
    combined_df = pd.concat(all_data, ignore_index=True)
    combined_df = combined_df.sort_values(['symbol', 'timestamp'])

    print(f'\nTotal rows: {len(combined_df):,}')
    print(f'Symbols found: {combined_df["symbol"].unique().tolist()}')
    print(f'Date range: {combined_df["timestamp"].min()} to {combined_df["timestamp"].max()}')

    # Save to CSV
    combined_df.to_csv(output_file, index=False)
    print(f'\nSaved to {output_file}')

    # Show sample
    print('\nFirst 10 rows:')
    print(combined_df.head(10)[['symbol', 'timestamp', 'borrow_rate_annual', 'availability']])

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Export historic borrow data for symbols')
    parser.add_argument('--bucket', default='ibkr-borrow-collector-borrowdatabucket-u0yupnyt837q',
                       help='S3 bucket name')
    parser.add_argument('--market', default='germany', help='Market name')
    parser.add_argument('--symbols', nargs='+', default=['SAP', 'BMW', 'DBK'],
                       help='Symbols to export')
    parser.add_argument('--output', default='/tmp/borrow_historic.csv',
                       help='Output CSV file')

    args = parser.parse_args()
    export_symbols(args.bucket, args.market, args.symbols, args.output)
