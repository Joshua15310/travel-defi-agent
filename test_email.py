import os
import sib_api_v3_sdk
from sib_api_v3_sdk.rest import ApiException

# 1. Setup Configuration
configuration = sib_api_v3_sdk.Configuration()
configuration.api_key['api-key'] = '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' # Paste your key here

# 2. Create an instance of the API class
api_instance = sib_api_v3_sdk.TransactionalEmailsApi(sib_api_v3_sdk.ApiClient(configuration))

# 3. Define the email content
send_smtp_email = sib_api_v3_sdk.SendSmtpEmail(
    to=[{"email": "odogun3@gmail.com", "name": "Tester"}],
    sender={"name": "Warden Travel Agent", "email": "warden.bookings@outlook.com"},
    subject="Hello from Warden Agent!",
    html_content="""
    <html>
        <body>
            <h1>Test Successful! 🚀</h1>
            <p>If you are reading this, your Brevo API key is working and the Warden Agent is ready to send receipts.</p>
        </body>
    </html>
    """
)

# 4. Send the email
try:
    api_response = api_instance.send_transac_email(send_smtp_email)
    print(f"✅ Success! Message ID: {api_response.message_id}")
except ApiException as e:
    print(f"❌ Failed to send: {e}")