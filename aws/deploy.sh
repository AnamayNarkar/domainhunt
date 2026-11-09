#!/usr/bin/env bash
# Deploys the hosted version: S3 website + Lambda (every minute via EventBridge) + optional delete-everything button.
# Needs: aws cli (configured), python3, zip, and the CLI deps (pip install -r cli/requirements.txt).
# Required env: CF_ACCOUNT_ID, CF_API_TOKEN.  Optional: TEARDOWN_PASSWORD (enables the delete button), REGION, BUCKET.
set -euo pipefail
: "${CF_ACCOUNT_ID:?set CF_ACCOUNT_ID}"; : "${CF_API_TOKEN:?set CF_API_TOKEN}"
REGION="${REGION:-us-east-1}"; export AWS_DEFAULT_REGION="$REGION"
HERE="$(cd "$(dirname "$0")" && pwd)"; BUILD="$HERE/build"; mkdir -p "$BUILD/pkg" "$BUILD/td"
ACCT="$(aws sts get-caller-identity --query Account --output text)"
B="${BUCKET:-domainhunt-$ACCT}"; ORIGIN="http://$B.s3-website-$REGION.amazonaws.com"
TRUST='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
BASIC=arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

echo "== word list (from the local CLI's filtered candidates) =="
python3 "$HERE/../cli/domainhunt.py" --dry-run --top 0 --profile "${PROFILE:-rare}" --out "$BUILD/cand"
python3 -c "import csv,sys;print('\n'.join(r['word'] for r in csv.DictReader(open('$BUILD/cand/candidates.csv'))))" > "$BUILD/pkg/words.txt"
wc -l "$BUILD/pkg/words.txt"
cp "$HERE/handler.py" "$BUILD/pkg/"; (cd "$BUILD/pkg" && rm -f ../fn.zip && zip -qr ../fn.zip .)

echo "== S3 website (public read for index.html + results.json only; state/ stays private) =="
if [ "$REGION" = us-east-1 ]; then aws s3api create-bucket --bucket "$B" >/dev/null
else aws s3api create-bucket --bucket "$B" --create-bucket-configuration LocationConstraint="$REGION" >/dev/null; fi
aws s3api put-public-access-block --bucket "$B" --public-access-block-configuration BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false
aws s3api put-bucket-policy --bucket "$B" --policy "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Principal\":\"*\",\"Action\":\"s3:GetObject\",\"Resource\":[\"arn:aws:s3:::$B/index.html\",\"arn:aws:s3:::$B/results.json\"]}]}"
aws s3 website "s3://$B" --index-document index.html

echo "== checker Lambda =="
aws iam create-role --role-name domainhunt-role --assume-role-policy-document "$TRUST" >/dev/null
aws iam attach-role-policy --role-name domainhunt-role --policy-arn "$BASIC"
aws iam put-role-policy --role-name domainhunt-role --policy-name s3 --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
 {\"Effect\":\"Allow\",\"Action\":[\"s3:GetObject\",\"s3:PutObject\"],\"Resource\":\"arn:aws:s3:::$B/*\"},
 {\"Effect\":\"Allow\",\"Action\":\"s3:ListBucket\",\"Resource\":\"arn:aws:s3:::$B\"},
 {\"Effect\":\"Allow\",\"Action\":[\"events:RemoveTargets\",\"events:DeleteRule\"],\"Resource\":\"arn:aws:events:$REGION:$ACCT:rule/domainhunt-every-minute\"}]}"
sleep 12   # IAM propagation
aws lambda create-function --function-name domainhunt --runtime python3.12 --handler handler.handler --timeout 60 --memory-size 128 \
  --role "arn:aws:iam::$ACCT:role/domainhunt-role" --zip-file "fileb://$BUILD/fn.zip" \
  --environment "Variables={BUCKET=$B,CF_ACCOUNT_ID=$CF_ACCOUNT_ID,CF_API_TOKEN=$CF_API_TOKEN}" >/dev/null
aws lambda put-function-concurrency --function-name domainhunt --reserved-concurrent-executions 1 >/dev/null || echo "(skipped reserved concurrency; new accounts may not allow it)"
aws events put-rule --name domainhunt-every-minute --schedule-expression 'rate(1 minute)' >/dev/null
aws lambda add-permission --function-name domainhunt --statement-id ev --action lambda:InvokeFunction --principal events.amazonaws.com \
  --source-arn "arn:aws:events:$REGION:$ACCT:rule/domainhunt-every-minute" >/dev/null
aws events put-targets --rule domainhunt-every-minute --targets "Id=1,Arn=arn:aws:lambda:$REGION:$ACCT:function:domainhunt" >/dev/null

TD_URL=""
if [ -n "${TEARDOWN_PASSWORD:-}" ]; then
  echo "== delete-everything Lambda (public URL protected only by TEARDOWN_PASSWORD - make it long and random) =="
  cp "$HERE/teardown.py" "$BUILD/td/"; (cd "$BUILD/td" && rm -f ../td.zip && zip -q ../td.zip teardown.py)
  aws iam create-role --role-name domainhunt-teardown-role --assume-role-policy-document "$TRUST" >/dev/null
  aws iam attach-role-policy --role-name domainhunt-teardown-role --policy-arn "$BASIC"
  LG="arn:aws:logs:$REGION:$ACCT:log-group:/aws/lambda"
  aws iam put-role-policy --role-name domainhunt-teardown-role --policy-name td --policy-document "{\"Version\":\"2012-10-17\",\"Statement\":[
   {\"Effect\":\"Allow\",\"Action\":[\"events:RemoveTargets\",\"events:DeleteRule\"],\"Resource\":\"arn:aws:events:$REGION:$ACCT:rule/domainhunt-every-minute\"},
   {\"Effect\":\"Allow\",\"Action\":\"lambda:DeleteFunction\",\"Resource\":[\"arn:aws:lambda:$REGION:$ACCT:function:domainhunt\",\"arn:aws:lambda:$REGION:$ACCT:function:domainhunt-teardown\"]},
   {\"Effect\":\"Allow\",\"Action\":[\"s3:ListBucket\",\"s3:DeleteBucket\"],\"Resource\":\"arn:aws:s3:::$B\"},
   {\"Effect\":\"Allow\",\"Action\":\"s3:DeleteObject\",\"Resource\":\"arn:aws:s3:::$B/*\"},
   {\"Effect\":\"Allow\",\"Action\":\"logs:DeleteLogGroup\",\"Resource\":[\"$LG/domainhunt\",\"$LG/domainhunt:*\",\"$LG/domainhunt-teardown\",\"$LG/domainhunt-teardown:*\"]},
   {\"Effect\":\"Allow\",\"Action\":[\"iam:DetachRolePolicy\",\"iam:DeleteRolePolicy\",\"iam:DeleteRole\"],\"Resource\":[\"arn:aws:iam::$ACCT:role/domainhunt-role\",\"arn:aws:iam::$ACCT:role/domainhunt-teardown-role\"]}]}"
  sleep 12
  aws lambda create-function --function-name domainhunt-teardown --runtime python3.12 --handler teardown.handler --timeout 60 --memory-size 128 \
    --role "arn:aws:iam::$ACCT:role/domainhunt-teardown-role" --zip-file "fileb://$BUILD/td.zip" \
    --environment "Variables={BUCKET=$B,TEARDOWN_PASSWORD=$TEARDOWN_PASSWORD,ORIGIN=$ORIGIN}" >/dev/null
  aws lambda wait function-active --function-name domainhunt-teardown
  TD_URL="$(aws lambda create-function-url-config --function-name domainhunt-teardown --auth-type NONE \
    --cors "AllowOrigins=$ORIGIN,AllowMethods=POST,AllowHeaders=content-type" --query FunctionUrl --output text)"
  aws lambda add-permission --function-name domainhunt-teardown --statement-id url --action lambda:InvokeFunctionUrl --principal '*' --function-url-auth-type NONE >/dev/null
  aws lambda add-permission --function-name domainhunt-teardown --statement-id url2 --action lambda:InvokeFunction --principal '*' >/dev/null
fi

if [ -n "$TD_URL" ]; then sed "s#__TEARDOWN_URL__#$TD_URL#" "$HERE/index.html" > "$BUILD/index.html"
else sed "s#<button class=del id=del>Delete all resources</button>##" "$HERE/index.html" > "$BUILD/index.html"; fi
aws s3 cp "$BUILD/index.html" "s3://$B/index.html" --content-type text/html --cache-control no-cache >/dev/null
echo; echo "Done. Site: $ORIGIN  (first results appear after ~1-2 minutes)"
