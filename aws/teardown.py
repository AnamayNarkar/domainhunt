import hmac, json, os, time, boto3
from botocore.exceptions import ClientError

BUCKET = os.environ["BUCKET"]
PW = os.environ["TEARDOWN_PASSWORD"]
ORIGIN = os.environ["ORIGIN"]
R = "us-east-1"
SELF = os.environ["AWS_LAMBDA_FUNCTION_NAME"]

def ok(fn, *a, **k):
    try: return fn(*a, **k)
    except ClientError as e: print("skip", fn.__name__, e.response["Error"]["Code"])

def resp(code, body):
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}

def handler(event, ctx):
    try: pw = json.loads(event.get("body") or "{}").get("password", "")
    except Exception: pw = ""
    if not hmac.compare_digest(str(pw).encode(), PW.encode()):
        time.sleep(2); return resp(403, {"error": "wrong password"})
    ev, lam, s3, logs, iam = (boto3.client(x, region_name=R) for x in ("events", "lambda", "s3", "logs", "iam"))
    done = []
    ok(ev.remove_targets, Rule="domainhunt-every-minute", Ids=["1"]); ok(ev.delete_rule, Name="domainhunt-every-minute"); done.append("schedule")
    ok(lam.delete_function, FunctionName="domainhunt"); done.append("lambda")
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs: s3.delete_objects(Bucket=BUCKET, Delete={"Objects": objs})
    ok(s3.delete_bucket, Bucket=BUCKET); done.append("s3 bucket")
    for g in ("/aws/lambda/domainhunt", "/aws/lambda/" + SELF): ok(logs.delete_log_group, logGroupName=g)
    done.append("logs")
    ok(iam.detach_role_policy, RoleName="domainhunt-role", PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole")
    ok(iam.delete_role_policy, RoleName="domainhunt-role", PolicyName="s3"); ok(iam.delete_role, RoleName="domainhunt-role"); done.append("role")
    # remove the teardown function itself and its role last
    ok(lam.delete_function, FunctionName=SELF)
    for p in ("AWSLambdaBasicExecutionRole",): ok(iam.detach_role_policy, RoleName="domainhunt-teardown-role", PolicyArn="arn:aws:iam::aws:policy/service-role/" + p)
    ok(iam.delete_role_policy, RoleName="domainhunt-teardown-role", PolicyName="td"); ok(iam.delete_role, RoleName="domainhunt-teardown-role")
    done.append("teardown function + role")
    return resp(200, {"deleted": done})
