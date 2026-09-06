import os
from supabase import create_client, Client

# =====================================================================
# 1. DATABASE SECURITY CREDENTIALS
# =====================================================================
# In a full deployment, these are safely loaded from hidden environment variables.
# You can find these exact strings in your Supabase dashboard settings under API.
SUPABASE_URL = "https://supabase.com/dashboard/project/wupivkrdqgrzogdaoueu/settings/api-keys"
# CRITICAL SECURITY PROTOCOL: Use your master service_role key here.
# Because Row Level Security (RLS) is enabled, your background server engine
# needs this administrative token to bypass restrictions and write data freely.
SUPABASE_SERVICE_KEY = "sb_secret_yjY39ocnjMYZTm8cwlNvkQ_mkqETZni"

# Initialize the secure cloud client connection
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# =====================================================================
# 2. THE REAL-TIME DATABASE UPSERT CORE
# =====================================================================
def push_calculated_metrics_to_cloud(calculated_data: dict):
    """
    Takes the live data dictionary calculated by your scanner engine
    and pushes it instantly to your live_scan_cache table using an UPSERT command.
    """
    if calculated_data.get("status") != "success":
        print("⚠️ Ingestion skipped: Data payload contains operational error flags.")
        return

    try:
        # Build the exact data columns payload matching your Supabase SQL schema setup
        db_payload = {
            "ticker": calculated_data["ticker"],
            "pattern_detected": calculated_data.get("pattern", "triple strike"), # Default test pattern
            "current_price": calculated_data["live_price"],
            "current_rvol": calculated_data["relative_volume_rvol"],
            "gap_fill_probability": calculated_data.get("gap_fill_prob", 74.0), # Pre-calculated statistical cache
            "market_session": "regular",
            "updated_at": "now()" # Database forces timestamp synchronization
        }

        # The UPSERT Command: If the ticker is already in the table, it updates the metrics.
        # If it's a new ticker breaking out, it appends a clean row automatically.
        response = supabase_client.table("live_scan_cache") \
            .upsert(db_payload, on_conflict="ticker") \
            .execute()

        print(f"🚀 Cloud Sync Success: {calculated_data['ticker']} successfully written to FloatIQ database.")

    except Exception as e:
        print(f"❌ Critical Database Link Invalidation: {str(e)}")

# =====================================================================
# 3. EXPERIMENTING WITH A LIVE PIPELINE SURGE
# =====================================================================
if __name__ == "__main__":
    print("--- TESTING FLOATING CLOUD BRIDGE SYNCHRONIZATION ---")

    # Mocking up a simulated live success payload from your scanner code
    simulated_scanner_output = {
        "status": "success",
        "ticker": "NVDA",
        "live_price": 120.50,
        "relative_volume_rvol": 2.8, # Whale volume triggered!
        "gap_fill_prob": 74.0, # Custom conditional statistics percentage
        "pattern": "inverse head and shoulders"
    }

    # Fire the payload across the web networks straight into your secured Supabase grid
    push_calculated_metrics_to_cloud(simulated_scanner_output)