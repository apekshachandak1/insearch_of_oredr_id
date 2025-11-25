# db.py
import os
import mysql.connector
from dotenv import load_dotenv

load_dotenv()

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "127.0.0.1"),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME"),
    "charset": os.getenv("DB_CHARSET", "utf8mb4"),
}


def get_connection():
    return mysql.connector.connect(
        host=DB_CONFIG["host"],
        user=DB_CONFIG["user"],
        password=DB_CONFIG["password"],
        database=DB_CONFIG["database"],
        charset=DB_CONFIG["charset"],
    )


def normalize_phone(phone: str) -> str:
    """Remove spaces, +, -, etc. Keep only digits."""
    if not phone:
        return ""
    return "".join(ch for ch in phone if ch.isdigit())


def phone_matches(db_phone: str, wa_phone: str) -> bool:
    """
    Compare phone from DB and WhatsApp sender.
    Allow match by last 9 digits to handle +91, 0, etc.
    """
    db_norm = normalize_phone(db_phone)
    wa_norm = normalize_phone(wa_phone)
    if not db_norm or not wa_norm:
        return False

    if db_norm == wa_norm:
        return True

    return db_norm[-9:] == wa_norm[-9:]


def fetch_order_with_validation(order_id: int, wa_phone: str):
    """
    Fetch order from oc_order + oc_customer and validate phone.
    Returns (order_data_dict, authorized_bool).
    """
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Database connection error: {e}")
        raise Exception(f"Database connection failed: {str(e)}")
    
    try:
        cursor = conn.cursor(dictionary=True)

        # Change "oc_" to your prefix if needed
        sql = """
        SELECT 
            o.order_id,
            o.firstname,
            o.lastname,
            o.telephone AS order_phone,
            o.date_added,
            o.total,
            os.name AS status_name,
            c.telephone AS customer_phone
        FROM oc_order o
        LEFT JOIN oc_customer c ON o.customer_id = c.customer_id
        LEFT JOIN oc_order_status os 
             ON o.order_status_id = os.order_status_id 
            AND os.language_id = 1
        WHERE o.order_id = %s
        LIMIT 1
        """
        cursor.execute(sql, (order_id,))
        row = cursor.fetchone()

        if not row:
            return None, False  # not found

        db_phone = row.get("order_phone") or row.get("customer_phone") or ""
        authorized = phone_matches(db_phone, wa_phone)

        customer_name = ((row.get("firstname") or "") + " " + (row.get("lastname") or "")).strip()
        if not customer_name:
            customer_name = "Customer"

        tracking_url = (
            "https://www.ipshopy.com/index.php?"
            f"route=account/order/info&order_id={row['order_id']}"
        )

        order_data = {
            "order_id": str(row["order_id"]),
            "customer_name": customer_name,
            "status": row.get("status_name") or "Unknown",
            "total": str(row.get("total", "")),
            "date_added": str(row.get("date_added")),
            "tracking_url": tracking_url,
        }

        return order_data, authorized
    finally:
        conn.close()


def fetch_orders_for_automation(limit=None, order_status_id=None, days_back=None):
    """
    Fetch orders from database for automation.
    Returns list of orders with customer name and phone number.
    
    Args:
        limit: Maximum number of orders to fetch (None = all)
        order_status_id: Filter by order status ID (None = all statuses)
        days_back: Fetch orders from last N days (None = all orders)
    """
    try:
        conn = get_connection()
    except Exception as e:
        print(f"Database connection error: {e}")
        raise Exception(f"Database connection failed: {str(e)}")
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        sql = """
        SELECT 
            o.order_id,
            o.firstname,
            o.lastname,
            o.telephone AS order_phone,
            o.date_added,
            o.total,
            o.order_status_id,
            os.name AS status_name,
            c.telephone AS customer_phone
        FROM oc_order o
        LEFT JOIN oc_customer c ON o.customer_id = c.customer_id
        LEFT JOIN oc_order_status os 
             ON o.order_status_id = os.order_status_id 
            AND os.language_id = 1
        WHERE 1=1
        """
        
        params = []
        
        # Filter by order status
        if order_status_id is not None:
            sql += " AND o.order_status_id = %s"
            params.append(order_status_id)
        
        # Filter by days back
        if days_back is not None:
            sql += " AND o.date_added >= DATE_SUB(NOW(), INTERVAL %s DAY)"
            params.append(days_back)
        
        sql += " ORDER BY o.order_id DESC"
        
        # Add limit
        if limit is not None:
            sql += " LIMIT %s"
            params.append(limit)
        
        cursor.execute(sql, tuple(params))
        rows = cursor.fetchall()
        
        orders = []
        for row in rows:
            # Get phone number (prefer order phone, fallback to customer phone)
            phone = row.get("order_phone") or row.get("customer_phone") or ""
            
            # Skip if no phone number
            if not phone or len(phone.strip()) < 10:
                continue
            
            customer_name = ((row.get("firstname") or "") + " " + (row.get("lastname") or "")).strip()
            if not customer_name:
                customer_name = "Customer"
            
            tracking_url = (
                "https://www.ipshopy.com/index.php?"
                f"route=account/order/info&order_id={row['order_id']}"
            )
            
            order_data = {
                "order_id": str(row["order_id"]),
                "customer_name": customer_name,
                "phone": phone,
                "status": row.get("status_name") or "Unknown",
                "status_id": row.get("order_status_id"),
                "total": str(row.get("total", "")),
                "date_added": str(row.get("date_added")),
                "tracking_url": tracking_url,
            }
            
            orders.append(order_data)
        
        return orders
    finally:
        conn.close()
