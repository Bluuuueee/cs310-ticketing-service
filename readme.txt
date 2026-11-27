CS 310 - Concurrent Ticketing on AWS
=====================================

Use Python 3.10 or newer, AWS CLI v2, and a browser. Run commands in
PowerShell. The examples use the cs310 profile and us-east-2. Stop if a
command fails.

Open the project and check the AWS account
-------------------------------------------

Open PowerShell in the cloned or extracted ticketing-service folder.

    $account = aws sts get-caller-identity --profile cs310 --region us-east-2 --query Account --output text

Add a new event
---------------

    python -B scripts/seed.py --account $account `
        --event luna-vale-encore `
        --name "Luna Vale Encore Concert" `
        --start "2026-12-19 20:00" `
        --end "2026-12-19 22:00" `
        --timezone "America/Chicago" `
        --mode correct

Change the sample ID, name and times. The event ID must be new and use
lowercase letters, digits or hyphens. The script creates the event and
50 available seats. Enter the event ID in the website's Event box.

Deleting an old event
---------------------------

    python -B scripts/delete_event.py --account $account --event concert-1
    python -B scripts/delete_event.py --account $account --event concert-1 --execute

First AWS deployment
--------------------

    python -B scripts/setup.py --account $account --profile cs310 --region us-east-2 --gateway rest

This creates the REST API, Lambda functions, DynamoDB tables and SQS queues.
Then create an event using the command above.

Switch an existing HTTP API deployment to REST API
--------------------------------------------------

    python -B scripts/deploy_rest.py --account $account --profile cs310 --region us-east-2

Open the printed /prod/ URL after verification. This reuses the current
functions, tables and queues.

Update the frontend or API Lambda
---------------------------------

Edit client/web for the website or server/api for API code. Then run:

    python -B scripts/package.py --target api
    aws lambda update-function-code --function-name ticketing-api --zip-file fileb://dist/api.zip --profile cs310 --region us-east-2 --query FunctionName --output text
    aws lambda wait function-updated-v2 --function-name ticketing-api --profile cs310 --region us-east-2

Refresh the same /prod/ page. If you change REST API routes in
scripts/rest_gateway.py, rerun the script that created that gateway
(setup.py for a fresh REST deployment, deploy_rest.py for an attached one).

Update the checkout Worker
--------------------------

    python -B scripts/package.py --target worker
    aws lambda update-function-code --function-name ticketing-worker --zip-file fileb://dist/worker.zip --profile cs310 --region us-east-2 --query FunctionName --output text
    aws lambda wait function-updated-v2 --function-name ticketing-worker --profile cs310 --region us-east-2

If a change affects both functions, update the Worker first and the API next.

Open the demo or run it locally
-------------------------------

    $config = Get-Content -Raw config/ticketing.json | ConvertFrom-Json
    $config.api_url.TrimEnd('/') + '/'

Open the printed URL. For local frontend preview, run the command below
and visit http://127.0.0.1:8501. Both frontends use the AWS backend.

    python -B client/app.py

Optional confirmation email
---------------------------

Verify the sender and test recipient in SES in us-east-2. You can use the
same verified email for both. The script deploys the email-enabled functions. New receipts include the code
for each ticket and a calendar attachment.

    $sender = Read-Host 'Verified SES sender email'
    $recipient = Read-Host 'Verified SES recipient email'
    python -B scripts/enable_receipts.py --account $account --sender $sender --recipient $recipient

To repair only the SES permissions or retry an existing failed receipt,
use the corresponding command below. The retry asks for confirmation.

    python -B scripts/enable_receipts.py --account $account --sender $sender --recipient $recipient --permissions-only
    python -B scripts/retry_receipt.py --account $account --event luna-vale-encore --recipient $recipient

Local tests
-----------

    python -B -m unittest discover -s tests -v

Measured AWS comparison reports are in results/. Keep AWS credentials and
.secrets/ outside the repository and submission archive.
