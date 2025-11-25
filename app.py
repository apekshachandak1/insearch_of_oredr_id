# app.py
import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import requests

from db import fetch_order_with_validation, fetch_orders_for_automation

load_dotenv()

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change_this")
INTERAKT_API_KEY = os.getenv("INTERAKT_API_KEY")
DEFAULT_COUNTRY_CODE = os.getenv("DEFAULT_COUNTRY_CODE", "+91")

# Your WhatsApp template name & language in Interakt
INTERAKT_TEMPLATE_NAME = "insearch_of_order_id"
INTERAKT_TEMPLATE_LANG = "en"

app = Flask(__name__)


def verify_auth_header(req: request) -> bool:
    """
    Simple Bearer token check for security (optional).
    If you don't want this, you can remove this check.
    """
    auth = req.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        return False
    token = auth[len(prefix):].strip()
    return token == WEBHOOK_SECRET


def parse_phone_number(phone: str) -> tuple[str, str]:
    """
    Parse phone number to extract country code and number.
    Returns (country_code, phone_number).
    Handles formats like: +917588348865, 917588348865, 7588348865
    """
    phone = phone.strip()
    
    # Remove any spaces, dashes, etc.
    phone_clean = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    
    # If starts with +, try to extract country code
    if phone_clean.startswith("+"):
        phone_clean = phone_clean[1:]  # Remove +
        
        # Common country codes
        if phone_clean.startswith("91") and len(phone_clean) >= 12:
            # India: +91XXXXXXXXXX
            return "+91", phone_clean[2:]
        elif phone_clean.startswith("1") and len(phone_clean) >= 11:
            # US/Canada: +1XXXXXXXXXX
            return "+1", phone_clean[1:]
        else:
            # Default: assume first 2 digits are country code for +91
            if len(phone_clean) >= 12 and phone_clean.startswith("91"):
                return "+91", phone_clean[2:]
            # Otherwise use default country code
            return DEFAULT_COUNTRY_CODE, phone_clean
    
    # If starts with 91 and has 12+ digits, assume it's India without +
    if phone_clean.startswith("91") and len(phone_clean) >= 12:
        return "+91", phone_clean[2:]
    
    # Otherwise use default country code
    return DEFAULT_COUNTRY_CODE, phone_clean


def interakt_send_order_status(order_data: dict, phone_number: str):
    """
    Call Interakt Send Template API.
    Assumes template body like:
      Hi {{1}}, your order {{2}} is being processed.
    And button URL like:
      https://www.ipshopy.com/index.php?route=account/order/info&order_id={{1}}
    - {{1}} in body -> customer_name
    - {{2}} in body -> order_id
    - {{1}} in button URL -> order_id (via buttonValues[0][0])
    """

    if not INTERAKT_API_KEY:
        raise RuntimeError("INTERAKT_API_KEY not set in .env")

    url = "https://api.interakt.ai/v1/public/message/"

    headers = {
        "Authorization": f"Basic {INTERAKT_API_KEY}",
        "Content-Type": "application/json",
    }

    # Parse phone number to extract country code and number
    country_code, phone_num = parse_phone_number(phone_number)
    
    payload = {
        "countryCode": country_code,
        "phoneNumber": phone_num,
        "callbackData": f"order:{order_data['order_id']}",
        "type": "Template",
        "template": {
            "name": INTERAKT_TEMPLATE_NAME,
            "languageCode": INTERAKT_TEMPLATE_LANG,
            "bodyValues": [
                order_data["customer_name"],  # -> {{1}}
            ],
            "buttonValues": {
                # First button's param list - {{1}} in button URL
                "0": [
                    order_data["order_id"]
                ]
            },
        },
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    try:
        data = resp.json()
    except Exception:
        data = {"raw_text": resp.text}

    return resp.status_code, data


def send_batch_whatsapp_messages(orders, delay_seconds=1):
    """
    Send WhatsApp messages to multiple orders.
    
    Args:
        orders: List of order dictionaries with customer_name, phone, order_id
        delay_seconds: Delay between messages to avoid rate limiting
    
    Returns:
        Dictionary with success/failure counts and details
    """
    import time
    
    results = {
        "total": len(orders),
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "details": []
    }
    
    for order in orders:
        phone = order.get("phone", "")
        
        # Skip if no phone
        if not phone or len(phone.strip()) < 10:
            results["skipped"] += 1
            results["details"].append({
                "order_id": order["order_id"],
                "status": "skipped",
                "reason": "No phone number"
            })
            continue
        
        try:
            # Prepare order data for Interakt
            order_data = {
                "order_id": order["order_id"],
                "customer_name": order["customer_name"],
                "status": order.get("status", "Unknown"),
                "total": order.get("total", ""),
                "date_added": order.get("date_added", ""),
                "tracking_url": order.get("tracking_url", "")
            }
            
            status_code, interakt_resp = interakt_send_order_status(order_data, phone)
            
            if status_code in [200, 201]:
                results["success"] += 1
                results["details"].append({
                    "order_id": order["order_id"],
                    "phone": phone,
                    "customer_name": order["customer_name"],
                    "status": "success",
                    "whatsapp_status_code": status_code
                })
            else:
                results["failed"] += 1
                results["details"].append({
                    "order_id": order["order_id"],
                    "phone": phone,
                    "customer_name": order["customer_name"],
                    "status": "failed",
                    "whatsapp_status_code": status_code,
                    "error": interakt_resp
                })
            
            # Delay between messages to avoid rate limiting
            if delay_seconds > 0:
                time.sleep(delay_seconds)
                
        except Exception as e:
            results["failed"] += 1
            results["details"].append({
                "order_id": order["order_id"],
                "phone": phone,
                "customer_name": order["customer_name"],
                "status": "error",
                "error": str(e)
            })
    
    return results


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/debug/order/<int:order_id>", methods=["GET"])
def debug_order(order_id):
    """
    Debug endpoint to check if order exists in database.
    Helps diagnose database connection and table prefix issues.
    """
    try:
        from db import get_connection
        
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        
        # Try to find the order
        sql = "SELECT order_id, firstname, lastname, telephone, customer_id FROM oc_order WHERE order_id = %s LIMIT 1"
        cursor.execute(sql, (order_id,))
        row = cursor.fetchone()
        
        # Get some sample orders to see what exists
        cursor.execute("SELECT order_id, firstname, lastname, telephone FROM oc_order ORDER BY order_id DESC LIMIT 5")
        sample_orders = cursor.fetchall()
        
        # Get total count
        cursor.execute("SELECT COUNT(*) as total FROM oc_order")
        total_count = cursor.fetchone()["total"]
        
        # Get min and max order_id
        cursor.execute("SELECT MIN(order_id) as min_id, MAX(order_id) as max_id FROM oc_order")
        id_range = cursor.fetchone()
        
        # Also check what tables exist
        cursor.execute("SHOW TABLES LIKE 'oc_order'")
        table_exists = cursor.fetchone() is not None
        
        # Check if table prefix might be different
        cursor.execute("SHOW TABLES")
        all_tables = [list(t.values())[0] for t in cursor.fetchall()]
        order_tables = [t for t in all_tables if 'order' in t.lower()]
        
        conn.close()
        
        return jsonify({
            "order_id": order_id,
            "order_found": row is not None,
            "order_data": row if row else None,
            "oc_order_table_exists": table_exists,
            "all_order_tables": order_tables,
            "database": os.getenv("DB_NAME", "not_set"),
            "table_prefix": "oc_",
            "total_orders": total_count,
            "order_id_range": id_range,
            "sample_orders": sample_orders
        })
    except Exception as e:
        return jsonify({
            "error": str(e),
            "order_id": order_id,
            "database": os.getenv("DB_NAME", "not_set")
        }), 500


@app.route("/api/order-status", methods=["GET"])
def order_status():
    """
    CALL THIS URL:
      http://127.0.0.1:5000/api/order-status?order_id=178541&phone=+917588348865

    FLOW:
      1) Validate Authorization header (optional)
      2) Read order_id + phone from query
      3) Fetch from DB
      4) Validate phone vs order
      5) Send WhatsApp template via Interakt
      6) Return JSON result
    """

    # OPTIONAL: if you want to protect this endpoint with a secret:
    # Comment out the next 3 lines if you don't want auth.
    # if not verify_auth_header(request):
    #     return jsonify({"error": "Unauthorized"}), 403

    order_id = request.args.get("order_id")
    phone = request.args.get("phone")

    if not order_id:
        return jsonify({"error": "order_id required"}), 400
    if not phone:
        return jsonify({"error": "phone required"}), 400

    try:
        order_id_int = int(order_id)
    except ValueError:
        return jsonify({"error": "order_id must be integer"}), 400

    # 1) Fetch order from local DB
    try:
        order_data, authorized = fetch_order_with_validation(order_id_int, phone)
    except Exception as e:
        return jsonify({
            "found": False,
            "authorized": False,
            "order_id": order_id,
            "message": f"Database error: {str(e)}",
            "debug_hint": "Check /api/debug/order/" + str(order_id_int) + " for more details"
        }), 500

    if not order_data:
        return jsonify({
            "found": False,
            "authorized": False,
            "order_id": order_id,
            "message": "Order not found in database",
            "debug_hint": "Check /api/debug/order/" + str(order_id_int) + " to verify order exists and table prefix"
        }), 404

    if not authorized:
        return jsonify({
            "found": True,
            "authorized": False,
            "order_id": order_data["order_id"],
            "message": "Phone number does not match this order"
        }), 403

    # 2) Send WhatsApp message using Interakt
    status_code, interakt_resp = interakt_send_order_status(order_data, phone)

    # 3) Respond back with DB + Interakt status
    return jsonify({
        "found": True,
        "authorized": True,
        "order": order_data,
        "whatsapp_status_code": status_code,
        "interakt_response": interakt_resp
    }), status_code


@app.route("/api/automate/send-messages", methods=["POST", "GET"])
def automate_send_messages():
    """
    Automate WhatsApp message sending to multiple orders.
    
    Query Parameters (GET) or JSON Body (POST):
        - limit: Maximum number of orders to process (default: 100)
        - order_status_id: Filter by order status ID (optional)
        - days_back: Fetch orders from last N days (optional)
        - delay_seconds: Delay between messages (default: 1)
        - dry_run: If true, only fetch orders without sending (default: false)
    
    Example GET:
        http://127.0.0.1:5000/api/automate/send-messages?limit=10&days_back=7
    
    Example POST:
        {
            "limit": 10,
            "days_back": 7,
            "order_status_id": 1,
            "delay_seconds": 2,
            "dry_run": false
        }
    """
    try:
        # Get parameters from query string (GET) or JSON body (POST)
        if request.method == "GET":
            limit = request.args.get("limit", type=int)
            order_status_id = request.args.get("order_status_id", type=int)
            days_back = request.args.get("days_back", type=int)
            delay_seconds = request.args.get("delay_seconds", type=float, default=1.0)
            dry_run = request.args.get("dry_run", "").lower() in ("true", "1", "yes")
        else:
            data = request.get_json() or {}
            limit = data.get("limit")
            order_status_id = data.get("order_status_id")
            days_back = data.get("days_back")
            delay_seconds = data.get("delay_seconds", 1.0)
            dry_run = data.get("dry_run", False)
        
        # Fetch orders from database
        orders = fetch_orders_for_automation(
            limit=limit,
            order_status_id=order_status_id,
            days_back=days_back
        )
        
        if not orders:
            return jsonify({
                "success": False,
                "message": "No orders found matching the criteria",
                "orders_found": 0,
                "filters": {
                    "limit": limit,
                    "order_status_id": order_status_id,
                    "days_back": days_back
                }
            }), 404
        
        # If dry run, just return the orders without sending
        if dry_run:
            return jsonify({
                "success": True,
                "dry_run": True,
                "message": f"Found {len(orders)} orders (dry run - no messages sent)",
                "orders_found": len(orders),
                "orders": orders,
                "filters": {
                    "limit": limit,
                    "order_status_id": order_status_id,
                    "days_back": days_back
                }
            }), 200
        
        # Send WhatsApp messages
        results = send_batch_whatsapp_messages(orders, delay_seconds=delay_seconds)
        
        return jsonify({
            "success": True,
            "message": f"Processed {results['total']} orders",
            "summary": {
                "total": results["total"],
                "success": results["success"],
                "failed": results["failed"],
                "skipped": results["skipped"]
            },
            "details": results["details"],
            "filters": {
                "limit": limit,
                "order_status_id": order_status_id,
                "days_back": days_back
            }
        }), 200
        
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "message": "Failed to process automation"
        }), 500


@app.route("/api/automate/preview", methods=["GET"])
def automate_preview():
    """
    Preview orders that would be processed by automation.
    Same filters as /api/automate/send-messages but only returns order list.
    """
    try:
        limit = request.args.get("limit", type=int)
        order_status_id = request.args.get("order_status_id", type=int)
        days_back = request.args.get("days_back", type=int)
        
        orders = fetch_orders_for_automation(
            limit=limit,
            order_status_id=order_status_id,
            days_back=days_back
        )
        
        return jsonify({
            "orders_found": len(orders),
            "orders": orders,
            "filters": {
                "limit": limit,
                "order_status_id": order_status_id,
                "days_back": days_back
            }
        }), 200
        
    except Exception as e:
        return jsonify({
            "error": str(e),
            "message": "Failed to fetch orders"
        }), 500


if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=5000, debug=True)
