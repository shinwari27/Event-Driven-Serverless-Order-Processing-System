import azure.functions as func
import logging
import json
import re
import uuid
from datetime import datetime, timezone
import os
from azure.communication.email import EmailClient
from azure.data.tables import TableClient

app = func.FunctionApp()

# Quiet the noisy Azure SDK HTTP logging — keeps the terminal clean
#No more walls of HTTP headers, there will be only readable data in the terminal
logging.getLogger("azure.core.pipeline.policies.http_logging_policy").setLevel(logging.WARNING)
logging.getLogger("azure").setLevel(logging.WARNING)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

VALID_PRODUCTS = {
    "HP-PAV-15", "ASUS-VIV-S14", "ACER-NIT-15", "GATE-15", "DELL-INS-15",
    "LEN-IDE-5", "MS-SUR-6", "ASUS-ROG-G16", "HP-SPEC-X360", "LEN-X1-CAR",
}


# ========================================================================
# FUNCTION 1 — submit_order (HTTP Trigger)
# ========================================================================
@app.route(route="submit_order", methods=["POST"],
           auth_level=func.AuthLevel.ANONYMOUS)
@app.queue_output(arg_name="queue_out",
                  queue_name="orders-incoming",
                  connection="AzureWebJobsStorage")
def submit_order(req: func.HttpRequest, queue_out: func.Out[str]) -> func.HttpResponse:
    logging.info("submit_order received a request")

    # Tier 1: only confirm the request is valid JSON with required fields present.
    try:
        order = req.get_json()
    except ValueError:
        return func.HttpResponse(
            json.dumps({"status": "error", "message": "Request body must be valid JSON"}),
            status_code=400, mimetype="application/json",
        )

    required = ["name", "email", "product", "quantity"]
    missing = [f for f in required if order.get(f) in (None, "")]
    if missing:
        return func.HttpResponse(
            json.dumps({"status": "rejected",
                        "errors": [f"missing required fields: {', '.join(missing)}"]}),
            status_code=400, mimetype="application/json",
        )

    order["order_id"] = str(uuid.uuid4())
    order["submitted_at"] = datetime.now(timezone.utc).isoformat()
    queue_out.set(json.dumps(order))
    logging.info("Order %s enqueued to orders-incoming", order["order_id"])

    return func.HttpResponse(
        json.dumps({"status": "received", "order_id": order["order_id"]}),
        status_code=200, mimetype="application/json",
    )

# ========================================================================
# FUNCTION 2 — validate_order (Queue Trigger)
# Business validation + live inventory. Routes failures to orders-invalid,
# valid orders fan out to orders-to-email and orders-to-log.
# ========================================================================
@app.queue_trigger(arg_name="msg",
                   queue_name="orders-incoming",
                   connection="AzureWebJobsStorage")
@app.queue_output(arg_name="email_out",
                  queue_name="orders-to-email",
                  connection="AzureWebJobsStorage")
@app.queue_output(arg_name="log_out",
                  queue_name="orders-to-log",
                  connection="AzureWebJobsStorage")
@app.queue_output(arg_name="invalid_out",
                  queue_name="orders-invalid",
                  connection="AzureWebJobsStorage")
def validate_order(msg: func.QueueMessage,
                   email_out: func.Out[str],
                   log_out: func.Out[str],
                   invalid_out: func.Out[str]) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))
    logging.info("validate_order processing order %s", order.get("order_id"))

    PRICE_CATALOG = {
        "HP-PAV-15": 949.99, "ASUS-VIV-S14": 948.99, "ACER-NIT-15": 1499.99,
        "GATE-15": 749.99, "DELL-INS-15": 1199.99, "LEN-IDE-5": 649.99,
        "MS-SUR-6": 1299.99, "ASUS-ROG-G16": 1799.99, "HP-SPEC-X360": 1899.99,
        "LEN-X1-CAR": 1449.99,
    }

    errors = []

    # 1. Email format
    if not EMAIL_RE.match(order.get("email", "")):
        errors.append("invalid email format")

    # 2. Quantity must be a number > 0
    try:
        qty = int(order.get("quantity", 0))
        if qty <= 0:
            errors.append("quantity must be greater than 0")
            qty = 0
    except (ValueError, TypeError):
        errors.append("quantity must be a number")
        qty = 0

    # 3. Product must exist in catalog
    product = order.get("product")
    if product not in PRICE_CATALOG:
        errors.append("product not found in catalog")
    else:
        order["unit_price"] = PRICE_CATALOG[product]

        # 4. Inventory check + decrement (only if quantity was valid)
        if qty > 0:
            inv = TableClient.from_connection_string(
                conn_str=os.environ["AzureWebJobsStorage"],
                table_name="LaptopInventory")

            laptop = inv.get_entity("LAPTOP", product)
            stock = int(laptop.get("Stock", 0))

            if stock < qty:
                # Not enough stock — reject (routes to orders-invalid below)
                errors.append(f"out of stock (only {stock} left)")
            else:
                # Enough stock — decrement and save the new count
                new_stock = stock - qty
                laptop["Stock"] = new_stock
                inv.update_entity(laptop)
                logging.info("%s stock: %s -> %s", product, stock, new_stock)

                # --- Low-stock alert to the owner (only on a successful sale) ---
                LOW_STOCK_THRESHOLD = 2
                if new_stock <= LOW_STOCK_THRESHOLD:
                    try:
                        admin_client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])
                        admin_client.begin_send({
                            "senderAddress": os.environ["ACS_SENDER_ADDRESS"],
                            "recipients": {"to": [{"address": os.environ["ADMIN_EMAIL"],
                                                   "displayName": "Store Admin"}]},
                            "content": {
                                "subject": f"Low Stock Alert: {product}",
                                "plainText": (f"Low stock warning.\n\n"
                                              f"Product: {product}\n"
                                              f"Remaining stock: {new_stock}\n\n"
                                              f"Please restock soon."),
                            },
                        }).result()
                        logging.info("Low-stock alert sent for %s (stock=%s)", product, new_stock)
                    except Exception as e:
                        logging.warning("Failed to send low-stock alert: %s", e)
                # --- end low-stock alert ---

    # --- Invalid order → dead-letter queue ------------------------------
    if errors:
        order["status"] = "REJECTED"
        order["validation_errors"] = errors
        invalid_out.set(json.dumps(order))
        logging.warning("Order %s REJECTED: %s", order.get("order_id"), errors)
        return

    # --- Valid order → fan out to email + log ---------------------------
    order["status"] = "VALID"
    order["validated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(order)
    email_out.set(payload)
    log_out.set(payload)
    logging.info("Order %s VALID — fanned out to email + log", order["order_id"])



# ========================================================================
# FUNCTION 3 — send_confirmation_email (Queue Trigger)
# Wakes up on orders-to-email. Sends the customer a confirmation email
# through Azure Communication Services.
# ========================================================================
@app.queue_trigger(arg_name="msg",
                   queue_name="orders-to-email",
                   connection="AzureWebJobsStorage")
def send_confirmation_email(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))
    logging.info("send_confirmation_email processing order %s", order.get("order_id"))

    client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])

    short_id = order["order_id"][:8].upper()
    qty = int(order.get("quantity", 1))
    product = order.get("product", "")
    price = float(order.get("unit_price", 0))
    line_total = price * qty

    # One product row for the order table
    items_html = (
        f"<tr style='border-bottom:1px solid rgba(255,255,255,0.07)'>"
        f"<td style='padding:12px 8px;text-align:left'>{product}</td>"
        f"<td style='padding:12px 8px;text-align:center'>{qty}</td>"
        f"<td style='padding:12px 8px;text-align:right'>CAD ${price:.2f}</td>"
        f"<td style='padding:12px 8px;text-align:right'>CAD ${line_total:.2f}</td>"
        f"</tr>"
    )

    html_body = (
        "<!DOCTYPE html>"
        "<html><body style='margin:0;padding:0;background:#050B18;font-family:Arial,sans-serif'>"
        "<div style='max-width:560px;margin:40px auto;background:#0D1528;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.07)'>"
        # Header
        "<div style='background:linear-gradient(135deg,#1E3A5F,#0D1528);padding:32px 40px;border-bottom:1px solid rgba(255,255,255,0.07)'>"
        "<h1 style='margin:0;font-size:24px;font-weight:900;color:#3B82F6'>LaptopZone</h1>"
        "<p style='margin:4px 0 0;font-size:12px;color:#64748B'>Canada's Premier Laptop Destination</p>"
        "</div>"
        # Body
        "<div style='padding:32px 40px'>"
        "<div style='text-align:center;margin-bottom:24px'>"
        "<div style='display:inline-block;width:60px;height:60px;background:rgba(16,185,129,0.1);border-radius:50%;border:2px solid rgba(16,185,129,0.3);font-size:28px;line-height:60px;text-align:center'>&#9989;</div>"        f"<p style='color:#94A3B8;margin:0'>Hi {order.get('name','Customer')}, your order is on its way.</p>"
        "</div>"
        # Order number
        "<div style='background:#111E35;border-radius:10px;padding:14px;text-align:center;margin-bottom:24px;border:1px solid rgba(37,99,235,0.3)'>"
        "<div style='font-size:11px;color:#64748B;margin-bottom:4px'>ORDER NUMBER</div>"
        f"<div style='font-size:20px;font-weight:800;color:#3B82F6'>{short_id}</div>"
        "</div>"
        # Order table
        "<table style='width:100%;border-collapse:collapse;margin-bottom:20px'>"
        "<thead><tr style='background:#111E35'>"
        "<th style='padding:10px 8px;text-align:left;font-size:11px;color:#64748B'>PRODUCT</th>"
        "<th style='padding:10px 8px;text-align:center;font-size:11px;color:#64748B'>QTY</th>"
        "<th style='padding:10px 8px;text-align:right;font-size:11px;color:#64748B'>PRICE</th>"
        "<th style='padding:10px 8px;text-align:right;font-size:11px;color:#64748B'>TOTAL</th>"
        "</tr></thead>"
        f"<tbody style='color:#F1F5F9;font-size:14px'>{items_html}</tbody>"
        "<tfoot><tr>"
        "<td colspan='3' style='padding:12px 8px;text-align:right;font-weight:700;color:#94A3B8'>Order Total</td>"
        f"<td style='padding:12px 8px;text-align:right;font-weight:800;font-size:16px;color:#3B82F6'>CAD ${line_total:.2f}</td>"
        "</tr></tfoot>"
        "</table>"
        # Azure note
        "<div style='background:rgba(16,185,129,0.07);border:1px solid rgba(16,185,129,0.2);border-radius:9px;padding:12px;text-align:center;font-size:12px;color:#10B981'>"
        "&#9889; Powered by Azure Functions &middot; Azure Storage Queues &middot; Azure Communication Services"
        "</div>"
        "</div>"
        # Footer
        "<div style='padding:20px 40px;border-top:1px solid rgba(255,255,255,0.07);text-align:center'>"
        "<p style='margin:0;font-size:12px;color:#64748B'>&copy; 2026 LaptopZone &middot; Powered by Azure Serverless</p>"
        "</div>"
        "</div></body></html>"
    )

    plain_body = (
        f"Hi {order.get('name','Customer')},\n\n"
        f"Your order is confirmed!\n\n"
        f"Order Number: {short_id}\n"
        f"Product: {product}\n"
        f"Quantity: {qty}\n"
        f"Unit Price: CAD ${price:.2f}\n"
        f"Order Total: CAD ${line_total:.2f}\n\n"
        f"Thank you for shopping with LaptopZone.\n"
    )

    message = {
        "senderAddress": os.environ["ACS_SENDER_ADDRESS"],
        "recipients": {"to": [{"address": order["email"], "displayName": order.get("name", "Customer")}]},
        "content": {
            "subject": f"Order {short_id} Confirmed - LaptopZone",
            "plainText": plain_body,
            "html": html_body,
        },
    }

    poller = client.begin_send(message)
    result = poller.result()
    logging.info("Email sent for order %s - status: %s", order["order_id"], result["status"])


# ========================================================================
# FUNCTION — send_rejection_email (Queue Trigger)
# Wakes up on orders-invalid. Tells the customer their order failed and why.
# ========================================================================
@app.queue_trigger(arg_name="msg",
                   queue_name="orders-invalid",
                   connection="AzureWebJobsStorage")
def send_rejection_email(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))
    logging.info("send_rejection_email processing order %s", order.get("order_id"))

    # If the email itself was invalid, there's no valid address to notify.
    email = order.get("email", "")
    if not EMAIL_RE.match(email):
        logging.warning("Order %s has an invalid email - cannot send rejection notice",
                        order.get("order_id"))
        return

    client = EmailClient.from_connection_string(os.environ["ACS_CONNECTION_STRING"])

    reasons = order.get("validation_errors", ["your order could not be processed"])
    reason_text = "; ".join(reasons)
    short_id = order.get("order_id", "")[:8].upper()
    name = order.get("name", "Customer")
    product = order.get("product", "")

    html_body = (
        "<!DOCTYPE html>"
        "<html><body style='margin:0;padding:0;background:#050B18;font-family:Arial,sans-serif'>"
        "<div style='max-width:560px;margin:40px auto;background:#0D1528;border-radius:16px;overflow:hidden;border:1px solid rgba(255,255,255,0.07)'>"
        # Header
        "<div style='background:linear-gradient(135deg,#1E3A5F,#0D1528);padding:32px 40px;border-bottom:1px solid rgba(255,255,255,0.07)'>"
        "<h1 style='margin:0;font-size:24px;font-weight:900;color:#3B82F6'>LaptopZone</h1>"
        "<p style='margin:4px 0 0;font-size:12px;color:#64748B'>Canada's Premier Laptop Destination</p>"
        "</div>"
        # Body
        "<div style='padding:32px 40px'>"
        "<div style='text-align:center;margin-bottom:24px'>"
        "<div style='display:inline-block;width:60px;height:60px;background:rgba(239,68,68,0.1);border-radius:50%;border:2px solid rgba(239,68,68,0.3);font-size:28px;line-height:60px;text-align:center'>&#9888;</div>"        "<h2 style='color:#F1F5F9;margin:16px 0 4px'>Order Could Not Be Processed</h2>"
        f"<p style='color:#94A3B8;margin:0'>Hi {name}, unfortunately there was a problem with your order.</p>"
        "</div>"
        # Order number
        "<div style='background:#111E35;border-radius:10px;padding:14px;text-align:center;margin-bottom:24px;border:1px solid rgba(239,68,68,0.3)'>"
        "<div style='font-size:11px;color:#64748B;margin-bottom:4px'>ORDER NUMBER</div>"
        f"<div style='font-size:20px;font-weight:800;color:#3B82F6'>{short_id}</div>"
        "</div>"
        # Reason box
        "<div style='background:rgba(239,68,68,0.07);border:1px solid rgba(239,68,68,0.25);border-radius:10px;padding:16px;margin-bottom:20px'>"
        "<div style='font-size:11px;color:#64748B;margin-bottom:6px'>REASON</div>"
        f"<div style='font-size:14px;color:#F1F5F9'>{reason_text}</div>"
        f"<div style='font-size:12px;color:#94A3B8;margin-top:8px'>Product: {product}</div>"
        "</div>"
        "<p style='color:#94A3B8;font-size:13px;text-align:center;margin:0 0 20px'>Please review your order details and try again. We're sorry for the inconvenience.</p>"
        # Azure note
        "<div style='background:rgba(16,185,129,0.07);border:1px solid rgba(16,185,129,0.2);border-radius:9px;padding:12px;text-align:center;font-size:12px;color:#10B981'>"
        "&#9889; Powered by Azure Functions &middot; Azure Storage Queues &middot; Azure Communication Services"
        "</div>"
        "</div>"
        # Footer
        "<div style='padding:20px 40px;border-top:1px solid rgba(255,255,255,0.07);text-align:center'>"
        "<p style='margin:0;font-size:12px;color:#64748B'>&copy; 2026 LaptopZone &middot; Powered by Azure Serverless</p>"
        "</div>"
        "</div></body></html>"
    )

    plain_body = (
        f"Hi {name},\n\n"
        f"Unfortunately, we were unable to process your order.\n\n"
        f"Order Number: {short_id}\n"
        f"Product: {product}\n"
        f"Reason: {reason_text}\n\n"
        f"Please review your order details and try again.\n"
    )

    message = {
        "senderAddress": os.environ["ACS_SENDER_ADDRESS"],
        "recipients": {"to": [{"address": email, "displayName": name}]},
        "content": {
            "subject": f"Order {short_id} Could Not Be Processed - LaptopZone",
            "plainText": plain_body,
            "html": html_body,
        },
    }

    poller = client.begin_send(message)
    result = poller.result()
    logging.info("Rejection email sent for order %s - status: %s",
                 order.get("order_id"), result["status"])  


# ========================================================================
# FUNCTION — log_to_table (Queue Trigger)
# Wakes up on orders-to-log. Permanently saves the order to Azure Table Storage.
# ========================================================================
@app.queue_trigger(arg_name="msg",
                   queue_name="orders-to-log",
                   connection="AzureWebJobsStorage")
def log_to_table(msg: func.QueueMessage) -> None:

    order = json.loads(msg.get_body().decode("utf-8"))
    logging.info("log_to_table processing order %s", order.get("order_id"))

    table = TableClient.from_connection_string(
        conn_str=os.environ["AzureWebJobsStorage"],
        table_name="Orders",
    )

    entity = {
        # PartitionKey = order date (YYYY-MM-DD), RowKey = unique order id
        "PartitionKey": order["submitted_at"][:10],
        "RowKey": order["order_id"],
        "CustomerName": order.get("name", ""),
        "CustomerEmail": order.get("email", ""),
        "Product": order.get("product", ""),
        "Quantity": int(order.get("quantity", 0)),
        "UnitPrice": float(order.get("unit_price", 0)),
        "SubmittedAt": order.get("submitted_at", ""),
        "ValidatedAt": order.get("validated_at", ""),
        "Status": order.get("status", "VALID"),
    }

    table.create_entity(entity=entity)
    logging.info("Order %s saved to Orders table", order["order_id"])