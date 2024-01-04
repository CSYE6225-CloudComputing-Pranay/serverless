import boto3
import requests
from google.cloud import storage
from google.oauth2 import service_account
import json
import os
import logging
import base64
import datetime
import smtplib
from email.mime.text import MIMEText


def lambda_handler(event, context):
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Extract submission URL and user email from the SNS message
    message_str = event['Records'][0]['Sns']['Message']
    logger.info("message_str: %s", message_str)

    # Parse the message string as JSON
    message = json.loads(message_str)
    logger.info("message: %s", message)

    # Extract submission_url and user_email
    status = message['status']
    submission_url = message['submissionUrl']
    user_email = message['userEmail']
    assignment_id = message['assignmentId']
    first_name = message['first_name']
    last_name = message['last_name']
    attempt = message['attempt']
    logger.info("submission_url: %s", submission_url)
    logger.info("user_email: %s", user_email)
    logger.info("assignment_id: %s", assignment_id)

    # Download the submission from the submission_url
    response = requests.get(submission_url)

    google_creds_base64 = os.environ['GOOGLE_CREDENTIALS']
    google_creds_json = base64.b64decode(google_creds_base64).decode('utf-8')

    try:
        # Parse the JSON string into a dictionary
        google_creds = json.loads(google_creds_json)
    except json.JSONDecodeError as e:
        print("Error parsing JSON: ", e)
        logger.error("Error: ", e)
        print("JSON string: ", google_creds_json)
        logger.info("GOOGLE_CREDENTIALS: JSON " + google_creds_json)
        raise

    # Google Cloud authentication
    credentials = service_account.Credentials.from_service_account_info(google_creds)
    storage_client = storage.Client(credentials=credentials)
    bucket = storage_client.bucket(os.environ['GCP_BUCKET_NAME'])
    logger.info("GCP_BUCKET_NAME: " + os.environ['GCP_BUCKET_NAME'])
    source_email = os.environ.get('FROM_ADDRESS')
    logger.info("source_email : %s", source_email)

    # Mailgun Credentials
    mailgun_user_name = os.environ.get('MAILGUN_USERNAME')
    mailgun_key = os.environ.get('MAILGUN_SMTP_KEY')

    try:
        if status == "SUCCESS":
            response = requests.get(submission_url)
            file_content = response.content
            if response.status_code != 200 or not file_content:
                raise ValueError("Invalid URL or empty content")

            timestamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            directory_path = f"{user_email}/{assignment_id}/"
            unique_file_name = f"submission_{attempt}_{timestamp}.zip"
            full_path = directory_path + unique_file_name
            blob = bucket.blob(full_path)
            blob.upload_from_string(file_content)
            logger.info("full_path : %s", full_path)

            logger.info("Sending Email")

            success_body = "We are happy to notify you that your assignment submission has been received and accepted."\
                           "\n\nSubmission Path  - {}"

            success_body = success_body.format(full_path)
            logger.info("success_email_body : %s", success_body)

            # Send success email
            send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email,
                       "Submission Received Successfully", success_body)

            logger.info("Email Sent and updating dynamo DB")
            # Update DynamoDB
            update_dynamodb(user_email, assignment_id, submission_url, full_path, timestamp)

            logger.info("Table updated")
        elif status == "NO_CONTENT":
            logger.info("Sending Email for no content")
            # Send failure email
            send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email,
                       "Submission Failed - Empty File",
                       "Your Submission could not be accepted as the file does not have any content")
        elif status == "INVALID_URL":
            logger.info("Sending Email for invalid URL")
            # Send failure email
            send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email,
                       "Submission Failed - Invalid URL",
                       "Your Submission could not be accepted as the URL submitted does not contain a valid zip file")
        elif status == "MAX_ATTEMPTS":
            logger.info("Sending Email for max attempts")
            # Send failure email
            send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email,
                       "Submission Failed - max attempts reached",
                       "Your Submission could not be accepted as you have reached maximum number of attempts")
        elif status == "DEADLINE_PASSED":
            logger.info("Sending Email for deadline passed")
            # Send failure email
            send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email,
                       "Submission Failed - Deadline Passed",
                       "Your Submission could not be accepted as the has passed")
        else:
            raise ValueError("Non-success status received")

    except Exception as e:
        logger.error(f"Error in processing submission: {e}")
        send_email(mailgun_user_name, mailgun_key, user_email, submission_url, assignment_id, source_email, "Submission Error - Canvas",
                   "There was an error with your submission. Please ensure the URL is correct and the content is not empty.")


def send_email(mailgun_user_name, mailgun_key, user_email, first_name, last_name, submission_url, assignment_id, attempt, source_email, subject,
               body):
    print("Sending email ", user_email, submission_url, assignment_id, source_email, subject, body)
    # Mailgun parameters
    logger = logging.getLogger()

    message_body = "Hello {} {}," \
                   "\n\n" \
                   "Your submission with assignment ID {} has been processed" \
                   "\n" \
                   "{}" \
                   "\n" \
                   "Attempt  - {}" \
                   "\n\n" \
                   "Regards," \
                   "\n" \
                   "Team Assessment Inc."
    message_body = message_body.format(first_name, last_name, assignment_id, body, attempt)
    logger.info("email_body : %s", message_body)

    # SMTP
    msg = MIMEText(message_body)
    msg['Subject'] = subject
    msg['From'] = source_email
    msg['To'] = user_email

    s = smtplib.SMTP('smtp.mailgun.org', 587)

    try:
        s.login(mailgun_user_name, mailgun_key)
        s.sendmail(msg['From'], msg['To'], msg.as_string())
        logger.info(f"Email sent successfully to {user_email}")

    except smtplib.SMTPException as e:
        logger.error(f"SMTPException: {e}")
    s.quit()


def update_dynamodb(user_email, assignment_id, submission_url, full_path, timestamp):
    table_name = os.environ.get('DYNAMO_TABLE_NAME')
    partition_key = f"{user_email}#{assignment_id}#{timestamp}"
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(table_name)
    table.put_item(
        Item={
            'Id': partition_key,
            'AssignmentId': assignment_id,
            'SubmissionUrl': submission_url,
            'FilePath': full_path,
            'Timestamp': timestamp
        }
    )
