# main.py - Complete LangChain Property Scraper System

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Optional
import asyncio
import aiohttp
from datetime import datetime, timedelta
import json
import os
import re
import logging
import traceback
from docx import Document
from docxtpl import DocxTemplate
import tempfile
import zipfile
from pathlib import Path
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, Inches

# LangChain imports
from langchain.agents import create_openai_functions_agent, AgentExecutor
from langchain.tools import tool
from langchain_openai import ChatOpenAI
from langchain.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_community.document_loaders import AsyncHtmlLoader
from langchain_community.document_transformers import Html2TextTransformer

# Airtop browser automation
from airtop import AsyncAirtop, SessionConfigV1, PageQueryConfig

# Configure logging for Railway
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="LOI Generator - LangChain Edition")

# Add CORS middleware BEFORE routes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify your frontend domain
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global exception handler
@app.exception_handler(Exception)
async def general_exception_handler(request, exc):
    logger.error(f"Unhandled exception: {exc}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=500,
        content={"detail": f"Internal server error: {str(exc)}"}
    )

# Get API keys from environment variables
OPENAI_KEY = os.getenv("OPENAI_API_KEY")
AIRTOP_KEY = os.getenv("AIRTOP_API_KEY")

# Validate API keys exist
if not OPENAI_KEY:
    print("Warning: Missing OPENAI_API_KEY environment variable! Some features may not work.")
if not AIRTOP_KEY:
    print("Warning: Missing AIRTOP_API_KEY environment variable! Browser automation will not work.")

# Initialize LLM (only if API key is available)
llm = None
if OPENAI_KEY:
    try:
        llm = ChatOpenAI(
            api_key=OPENAI_KEY,
            model="gpt-4o-mini",
            temperature=0
        )
    except Exception as e:
        print(f"Error initializing OpenAI: {e}")

# Initialize Airtop (only if API key is available)
airtop_client = None
if AIRTOP_KEY:
    try:
        airtop_client = AsyncAirtop(api_key=AIRTOP_KEY)
    except Exception as e:
        print(f"Error initializing Airtop: {e}")

# Simple in-memory cache
property_cache = {}

# Request models
class PropertyRequest(BaseModel):
    address: str
    
class BatchRequest(BaseModel):
    addresses: List[str]
    email: Optional[str] = None

# Response models
class PropertyData(BaseModel):
    address: str
    owner_name: str
    owner_mailing_address: str
    listing_price: float
    last_sale_price: Optional[float]
    property_details: Dict
    calculations: Dict
    scraped_at: datetime

# County Scraper Agent
class CountyScraperAgent:
    def __init__(self):
        self.airtop = airtop_client
        
    async def scrape_fulton_county(self, address: str) -> Dict:
        """Scrapes Fulton County, GA assessor for owner info"""
        if not self.airtop:
            raise Exception("Airtop client not available - AIRTOP_API_KEY required")
            
        session = None
        try:
            # Create session with new API
            config = SessionConfigV1(timeout_minutes=15)
            session = await self.airtop.sessions.create(configuration=config)
            session_id = session.data.id
            
            # Create window
            window = await self.airtop.windows.create(
                session_id, 
                url="https://qpublic.schneidercorp.com/Application.aspx?App=FultonCountyGA&Layer=Parcels&PageType=Search"
            )
            window_id = window.data.window_id
            
            # Wait for page load
            await asyncio.sleep(3)
            
            # Use page_query to actually scrape the data
            result = await self.airtop.windows.page_query(
                session_id=session_id,
                window_id=window_id,
                prompt=f"""
                Navigate to the Fulton County assessor search page.
                Type "{address}" into the address search field.
                Click the search button.
                Wait for results to load.
                Click on the first property result.
                Extract the following data from the property details page:
                - Owner name (look for "Owner" field)
                - Owner mailing address (look for "Mailing Address" field)
                - Parcel ID (look for "Parcel ID" field)
                - Property class (look for "Property Class" field)
                
                Return ONLY the extracted data as JSON with these exact keys:
                {{
                    "owner_name": "actual owner name from page",
                    "owner_mailing_address": "actual mailing address from page", 
                    "parcel_id": "actual parcel ID from page",
                    "property_class": "actual property class from page"
                }}
                
                Do NOT make up any data. Only return what you can actually see on the page.
                """,
                configuration=PageQueryConfig()
            )
            
            # Parse the actual result from Airtop
            if result and hasattr(result, 'data') and result.data:
                try:
                    # Try to parse the JSON response from Airtop
                    if isinstance(result.data, str):
                        scraped_data = json.loads(result.data)
                    else:
                        scraped_data = result.data
                    
                    # Validate we got real data
                    if scraped_data.get("owner_name") and scraped_data.get("owner_name") != "John Smith":
                        return {
                            "owner_name": scraped_data.get("owner_name", "Unknown"),
                            "owner_mailing_address": scraped_data.get("owner_mailing_address", "Unknown"),
                            "parcel_id": scraped_data.get("parcel_id", "Unknown"),
                            "property_class": scraped_data.get("property_class", "Unknown"),
                            "source": "Fulton County Assessor (Airtop)"
                        }
                    else:
                        raise Exception("No real owner data found")
                        
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"Failed to parse Airtop result: {e}")
                    raise Exception("Failed to parse scraped data")
            else:
                raise Exception("No data returned from Airtop scraping")
            
        except Exception as e:
            error_msg = str(e)
            if "limit" in error_msg.lower() or "session" in error_msg.lower():
                logger.error(f"Airtop session limit reached: {error_msg}")
                raise Exception("Airtop free plan session limit reached. Please upgrade your Airtop plan or wait for active sessions to expire.")
            else:
                logger.error(f"Fulton scraping error: {error_msg}")
                raise Exception(f"Failed to scrape Fulton County data: {error_msg}")
        finally:
            if session:
                try:
                    await self.airtop.sessions.terminate(session.data.id)
                    logger.info(f"Terminated Airtop session: {session.data.id}")
                except Exception as e:
                    logger.error(f"Failed to terminate session: {str(e)}")
                    pass
    
    async def scrape_la_county(self, address: str) -> Dict:
        """Scrapes LA County assessor for owner info"""
        if not self.airtop:
            raise Exception("Airtop client not available - AIRTOP_API_KEY required")
            
        session = None
        try:
            # Create session with new API
            config = SessionConfigV1(timeout_minutes=15)
            session = await self.airtop.sessions.create(configuration=config)
            session_id = session.data.id
            
            # Create window
            window = await self.airtop.windows.create(
                session_id, 
                url="https://assessor.lacounty.gov/"
            )
            window_id = window.data.window_id
            
            # Wait for page load
            await asyncio.sleep(3)
            
            # Use page_query to actually scrape the data
            result = await self.airtop.windows.page_query(
                session_id=session_id,
                window_id=window_id,
                prompt=f"""
                Navigate to the LA County assessor website.
                Find and click on the "Property Search" link.
                Wait for the search page to load.
                Type "{address}" into the address field.
                Click the search button.
                Wait for results to load.
                Click on the first property result.
                Extract the following data from the property details page:
                - Owner name (look for "Owner" field)
                - Owner mailing address (look for "Mailing Address" field)
                
                Return ONLY the extracted data as JSON with these exact keys:
                {{
                    "owner_name": "actual owner name from page",
                    "owner_mailing_address": "actual mailing address from page"
                }}
                
                Do NOT make up any data. Only return what you can actually see on the page.
                """,
                configuration=PageQueryConfig()
            )
            
            # Parse the actual result from Airtop
            if result and hasattr(result, 'data') and result.data:
                try:
                    # Try to parse the JSON response from Airtop
                    if isinstance(result.data, str):
                        scraped_data = json.loads(result.data)
                    else:
                        scraped_data = result.data
                    
                    # Validate we got real data
                    if scraped_data.get("owner_name") and scraped_data.get("owner_name") != "Jane Doe":
                        return {
                            "owner_name": scraped_data.get("owner_name", "Unknown"),
                            "owner_mailing_address": scraped_data.get("owner_mailing_address", "Unknown"),
                            "source": "LA County Assessor (Airtop)"
                        }
                    else:
                        raise Exception("No real owner data found")
                        
                except (json.JSONDecodeError, KeyError) as e:
                    logger.error(f"Failed to parse Airtop result: {e}")
                    raise Exception("Failed to parse scraped data")
            else:
                raise Exception("No data returned from Airtop scraping")
            
        except Exception as e:
            error_msg = str(e)
            if "limit" in error_msg.lower() or "session" in error_msg.lower():
                logger.error(f"Airtop session limit reached: {error_msg}")
                raise Exception("Airtop free plan session limit reached. Please upgrade your Airtop plan or wait for active sessions to expire.")
            else:
                logger.error(f"LA County scraping error: {error_msg}")
                raise Exception(f"Failed to scrape LA County data: {error_msg}")
        finally:
            if session:
                try:
                    await self.airtop.sessions.terminate(session.data.id)
                    logger.info(f"Terminated Airtop session: {session.data.id}")
                except Exception as e:
                    logger.error(f"Failed to terminate session: {str(e)}")
                    pass

# Zillow Scraper Agent  
class ZillowScraperAgent:
    def __init__(self):
        self.airtop = airtop_client
        
    async def get_listing_price(self, address: str) -> Dict:
        """Scrapes Zillow for current listing price"""
        if not self.airtop:
            raise Exception("Airtop client not available - AIRTOP_API_KEY required")
            
        session = None
        try:
            # Create session with new API
            config = SessionConfigV1(timeout_minutes=15)
            session = await self.airtop.sessions.create(configuration=config)
            session_id = session.data.id
            
            # Create window
            window = await self.airtop.windows.create(
                session_id, 
                url="https://www.zillow.com/"
            )
            window_id = window.data.window_id
            
            # Wait for page load
            await asyncio.sleep(3)
            
            # Use page_query to actually scrape the data
            result = await self.airtop.windows.page_query(
                session_id=session_id,
                window_id=window_id,
                prompt=f"""
                Navigate to Zillow's homepage.
                Type "{address}" into the search field.
                Press Enter to search.
                Wait for results to load.
                
                Look for the property listing that matches "{address}" exactly.
                Extract the following data from the property listing:
                - Listing price (look for the main price display, usually in large text)
                - Number of bedrooms (look for "bed" or "beds")
                - Number of bathrooms (look for "bath" or "baths") 
                - Square footage (look for "sqft" or "square feet")
                
                Return ONLY the extracted data as JSON with these exact keys:
                {{
                    "listing_price": actual_price_number,
                    "property_details": {{
                        "bedrooms": "actual_bedroom_count",
                        "bathrooms": "actual_bathroom_count",
                        "sqft": "actual_square_footage"
                    }}
                }}
                
                For the listing price, return ONLY the number (no $ or commas).
                For bedrooms/bathrooms/sqft, return the actual values you see.
                Do NOT make up any data. Only return what you can actually see on the page.
                If you cannot find a value, use "Unknown" for that field.
                """,
                configuration=PageQueryConfig()
            )
            
            # Parse the actual result from Airtop
            if result and hasattr(result, 'data') and result.data:
                try:
                    # Try to parse the JSON response from Airtop
                    if isinstance(result.data, str):
                        scraped_data = json.loads(result.data)
                    else:
                        scraped_data = result.data
                    
                    # Extract and validate the price
                    listing_price = scraped_data.get("listing_price")
                    if listing_price and listing_price != "Unknown":
                        # Convert to float if it's a string
                        if isinstance(listing_price, str):
                            listing_price = float(listing_price.replace('$', '').replace(',', ''))
                        
                        return {
                            "listing_price": listing_price,
                            "property_details": {
                                "bedrooms": scraped_data.get("property_details", {}).get("bedrooms", "Unknown"),
                                "bathrooms": scraped_data.get("property_details", {}).get("bathrooms", "Unknown"),
                                "sqft": scraped_data.get("property_details", {}).get("sqft", "Unknown")
                            },
                            "source": "Zillow (Airtop)"
                        }
                    else:
                        raise Exception("No real price data found")
                        
                except (json.JSONDecodeError, KeyError, ValueError) as e:
                    logger.error(f"Failed to parse Airtop result: {e}")
                    raise Exception("Failed to parse scraped data")
            else:
                raise Exception("No data returned from Airtop scraping")
            
        except Exception as e:
            error_msg = str(e)
            if "limit" in error_msg.lower() or "session" in error_msg.lower():
                logger.error(f"Airtop session limit reached: {error_msg}")
                raise Exception("Airtop free plan session limit reached. Please upgrade your Airtop plan or wait for active sessions to expire.")
            else:
                logger.error(f"Zillow scraping error: {error_msg}")
                raise Exception(f"Failed to scrape Zillow data: {error_msg}")
        finally:
            if session:
                try:
                    await self.airtop.sessions.terminate(session.data.id)
                    logger.info(f"Terminated Airtop session: {session.data.id}")
                except Exception as e:
                    logger.error(f"Failed to terminate session: {str(e)}")
                    pass

# LOI Calculator
class LOICalculator:
    @staticmethod
    def calculate_offer(listing_price: float, strategy: str = "standard") -> Dict:
        """Calculate offer price and terms based on listing price"""
        
        calculations = {
            "listing_price": listing_price,
            "offer_price": listing_price * 0.9,  # 90% of asking
            "earnest_money": listing_price * 0.01,  # 1% earnest money
            "down_payment": listing_price * 0.2,  # 20% down
            "loan_amount": listing_price * 0.72,  # 80% of offer price
        }
        
        # Estimate rent (rough calculation - 0.8-1% of value)
        calculations["estimated_monthly_rent"] = listing_price * 0.009
        
        # Calculate cap rate
        annual_rent = calculations["estimated_monthly_rent"] * 12
        calculations["cap_rate"] = (annual_rent / calculations["offer_price"]) * 100
        
        # Cash flow estimate (assuming 50% expense ratio)
        calculations["estimated_cash_flow"] = calculations["estimated_monthly_rent"] * 0.5
        
        return calculations

# Document Generator
class DocumentGenerator:
    @staticmethod
    def create_loi_docx(property_data: PropertyData) -> str:
        """Generate LOI document in .docx format matching the exact professional format"""
        
        # Create document
        doc = Document()
        
        # Format date to M/DD/YYYY
        today = datetime.now().strftime("%-m/%-d/%Y")
        accept_by = (datetime.now() + timedelta(days=7)).strftime("%-m/%-d/%Y")
        
        # Calculate additional fields needed for the template
        price = property_data.calculations["offer_price"]
        financing = property_data.calculations["loan_amount"]
        earnest1 = property_data.calculations["earnest_money"]
        earnest2 = earnest1 * 2  # Second earnest payment
        total_earnest = earnest1 + earnest2
        
        # Default buyer entity if not provided
        buyer_entity = "Your Investment Company LLC"
        
        # Add title with professional formatting
        title = doc.add_heading('Letter of Intent', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.style.font.size = Pt(14)
        title.style.font.bold = True
        
        # Add date
        date_para = doc.add_paragraph()
        date_run = date_para.add_run(f'DATE: {today}')
        date_run.bold = True
        
        # Add purchaser
        purchaser_para = doc.add_paragraph()
        purchaser_run = purchaser_para.add_run(f'Purchaser: {buyer_entity}')
        purchaser_run.bold = True
        
        # Add property reference
        prop_ref = doc.add_paragraph()
        prop_run = prop_ref.add_run(f'RE: {property_data.address} ("the Property")')
        prop_run.bold = True
        
        # Add introduction
        intro_para = doc.add_paragraph()
        intro_para.add_run('This ')
        intro_bold = intro_para.add_run('non-binding letter')
        intro_bold.bold = True
        intro_para.add_run(' represents Purchaser\'s intent to purchase the above captioned property (the "Property") including the land and improvements on the following terms and conditions:')
        
        # Create table for terms - NO BORDERS, clean layout
        table = doc.add_table(rows=0, cols=2)
        table.style = 'Table Normal'  # No borders
        table.autofit = False
        table.allow_autofit = False
        
        # Set column widths to match the image
        table.columns[0].width = Inches(1.8)
        table.columns[1].width = Inches(4.7)
        
        # Add terms rows with exact formatting from image
        def add_term_row(label, content):
            row = table.add_row()
            row.cells[0].text = label
            row.cells[1].text = content
            # Make label bold
            for paragraph in row.cells[0].paragraphs:
                for run in paragraph.runs:
                    run.bold = True
        
        def add_indent_row(content):
            row = table.add_row()
            row.cells[0].text = ""
            row.cells[1].text = content
            # No additional indentation - just aligned with content column
        
        # Add all the terms exactly as shown in image
        add_term_row("Price:", f"${price:,.0f}")
        add_term_row("Financing:", f"Purchaser intends to obtain a loan of roughly ${financing:,.2f} commercial financing priced at prevailing interest rates.")
        add_term_row("Earnest Money:", f"Concurrently with full execution of a Purchase & Sale Agreement, Purchaser shall make an earnest money deposit (\"The Initial Deposit\") with a mutually agreed upon escrow agent in the amount of USD ${earnest1:,.1f} to be held in escrow and applied to the purchase price at closing. On expiration of the Due Diligence, Purchaser will pay a further ${earnest2:,.1f} deposit towards the purchase price and the combined ${total_earnest:,.0f} will be fully non-refundable.")
        add_term_row("Due Diligence:", "Purchaser shall have 45 calendar days due diligence period from the time of the execution of a formal Purchase and Sale Agreement and receipt of relevant documents.")
        add_indent_row("Seller to provide all books and records within 3 business day of effective contract date, including HOA resale certificates, property disclosures, 3 years of financial statements, pending litigation, and all documentation related to sewage intrusion.")
        add_term_row("Title Contingency:", "Seller shall be ready, willing and able to deliver free and clear title to the Property at closing, subject to standard title exceptions acceptable to Purchaser.")
        add_indent_row("Purchaser to select title and escrow companies.")
        add_term_row("Appraisal Contingency:", "None")
        add_term_row("Buyer Contingency:", "Purchaser's obligation to purchase is contingent upon Purchaser's successful sale of its Ohio property as part of a Section 1031 like-kind exchange, with Seller agreeing to reasonably cooperate (at no additional cost or liability to Seller).")
        add_indent_row("Purchaser's obligation to purchase is contingent upon HOA approval of bulk sale.")
        add_term_row("Closing:", "Closing shall occur after completion of due diligence period on a date agreed to by Purchaser and Seller and further detailed in the Purchase and Sale Agreement. Closing shall not take place any sooner that 45 days from the execution of a formal Purchase and Sale Agreement.")
        add_indent_row("Purchaser and Seller agree to a one (1) time 15-day optional extension for closing.")
        add_term_row("Closing Costs:", "Purchaser shall pay the cost of obtaining a title commitment and an owner's policy of title insurance.")
        add_indent_row("Seller shall pay for documentary stamps on the deed conveying the Property to Purchaser.")
        add_indent_row("Seller and Listing Broker to execute a valid Brokerage Referral Agreement with Buyer's brokerage providing for 3% commission payable to Buyer's Brokerage.")
        add_term_row("Purchase Contract:", "Pending receipt of sufficient information from Seller, Purchaser shall have (5) business days from mutual execution of this Letter of Intent agreement to submit a purchase and sale agreement.")
        
        # Add closing paragraph with exact formatting from image
        doc.add_paragraph()
        closing_para = doc.add_paragraph()
        closing_para.add_run('This letter of intent is ')
        closing_bold = closing_para.add_run('not intended')
        closing_bold.bold = True
        closing_para.add_run(' to create a binding agreement on the Seller to sell or the Purchaser to buy. The purpose of this letter is to set forth the primary terms and conditions upon which to execute a formal Purchase and Sale Agreement. All other terms and conditions shall be negotiated in the formal Purchase and Sale Agreement. This letter of Intent is open for acceptance through ')
        closing_date = closing_para.add_run(accept_by)
        closing_date.bold = True
        closing_para.add_run('.')
        
        # Add signature blocks with exact spacing from image
        purchaser_sig = doc.add_paragraph(f"PURCHASER: {buyer_entity}")
        purchaser_sig.paragraph_format.space_after = Pt(12)
        
        doc.add_paragraph()
        doc.add_paragraph("By: _____________________________________ Date:________________")
        doc.add_paragraph()
        doc.add_paragraph("Name: _________________________________________________")
        doc.add_paragraph()
        
        agreed_para = doc.add_paragraph("Agreed and Accepted:")
        agreed_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
        agreed_para.paragraph_format.space_after = Pt(12)
        
        doc.add_paragraph()
        seller_sig = doc.add_paragraph(f"SELLER: {property_data.owner_name}")
        seller_sig.paragraph_format.space_after = Pt(12)
        
        doc.add_paragraph()
        doc.add_paragraph()
        doc.add_paragraph("By: _____________________________________ Date:________________")
        doc.add_paragraph()
        doc.add_paragraph("Name: _________________________________________________")
        doc.add_paragraph()
        doc.add_paragraph("Title: __________________________________________________")
        
        # Save to temp file
        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix='.docx')
        doc.save(temp_file.name)
        temp_file.close()
        
        return temp_file.name

# Main scraping orchestrator
async def scrape_property(address: str) -> PropertyData:
    """Main function to scrape all property data"""
    
    # Check cache first
    if address in property_cache:
        cached_data = property_cache[address]
        if (datetime.now() - cached_data.scraped_at).days < 7:
            return cached_data
    
    # Determine county based on address
    county_scraper = CountyScraperAgent()
    zillow_scraper = ZillowScraperAgent()
    
    # Parallel scraping
    if "GA" in address or "Georgia" in address:
        owner_task = county_scraper.scrape_fulton_county(address)
    elif "CA" in address or "California" in address:
        owner_task = county_scraper.scrape_la_county(address)
    else:
        raise ValueError("Currently only supporting GA and CA properties")
    
    price_task = zillow_scraper.get_listing_price(address)
    
    # Wait for both
    owner_info, price_info = await asyncio.gather(owner_task, price_task)
    
    # Calculate offer terms
    calculations = LOICalculator.calculate_offer(price_info["listing_price"])
    
    # Create property data object
    property_data = PropertyData(
        address=address,
        owner_name=owner_info["owner_name"],
        owner_mailing_address=owner_info["owner_mailing_address"],
        listing_price=price_info["listing_price"],
        last_sale_price=None,
        property_details=price_info.get("property_details", {}),
        calculations=calculations,
        scraped_at=datetime.now()
    )
    
    # Cache it
    property_cache[address] = property_data
    
    return property_data

# API Endpoints
@app.get("/")
def read_root():
    return {
        "service": "LOI Generator - LangChain Edition",
        "status": "Running with Airtop browser automation",
        "endpoints": [
            "/scrape-property",
            "/generate-loi",
            "/batch-process",
            "/health"
        ]
    }

@app.post("/scrape-property")
async def scrape_property_endpoint(request: PropertyRequest):
    """Scrape property data from county and Zillow"""
    try:
        logger.info(f"Starting scrape for address: {request.address}")
        property_data = await scrape_property(request.address)
        logger.info(f"Successfully scraped data for: {request.address}")
        return property_data
    except Exception as e:
        logger.error(f"Scrape property error: {str(e)}")
        logger.error(f"Error type: {type(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/generate-loi")
async def generate_loi_endpoint(request: PropertyRequest):
    """Generate LOI document for a property"""
    try:
        # Get property data
        property_data = await scrape_property(request.address)
        
        # Generate Word document
        docx_path = DocumentGenerator.create_loi_docx(property_data)
        
        # Return Word document file
        filename = f"LOI_{request.address.replace(' ', '_').replace(',', '')}.docx"
        return FileResponse(
            docx_path,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=filename
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/batch-process")
async def batch_process_endpoint(request: BatchRequest):
    """Process multiple properties and return ZIP"""
    try:
        # Create temp directory for files
        temp_dir = tempfile.mkdtemp()
        doc_files = []
        
        # Process each address
        for address in request.addresses:
            try:
                property_data = await scrape_property(address)
                docx_path = DocumentGenerator.create_loi_docx(property_data)
                
                # Save HTML to a temporary file
                filename = f"LOI_{address.replace(' ', '_').replace(',', '')}.docx"
                new_path = os.path.join(temp_dir, filename)
                os.rename(docx_path, new_path) # Rename the temporary file to the desired name
                doc_files.append(new_path)
                
            except Exception as e:
                print(f"Error processing {address}: {str(e)}")
                continue
        
        # Create ZIP file
        zip_path = os.path.join(temp_dir, "LOI_Package.zip")
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for doc_file in doc_files:
                zipf.write(doc_file, os.path.basename(doc_file))
        
        # Return ZIP file
        return FileResponse(
            zip_path,
            media_type="application/zip",
            filename=f"LOI_Package_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# Health check endpoint
@app.get("/health")
def health_check():
    return {
        "status": "healthy", 
        "timestamp": datetime.now().isoformat(),
        "env_vars_loaded": {
            "OPENAI_API_KEY": bool(OPENAI_KEY),
            "AIRTOP_API_KEY": bool(AIRTOP_KEY)
        },
        "mode": "airtop_browser_automation"
    }

# Test Airtop API endpoint
@app.get("/test-airtop")
async def test_airtop():
    """Test Airtop API directly"""
    try:
        if not airtop_client:
            return {"error": "Airtop client not initialized"}
        
        # Check what methods are available
        methods = [method for method in dir(airtop_client) if not method.startswith('_')]
        
        # Try a simple test with new API
        try:
            # Create session
            config = SessionConfigV1(timeout_minutes=5)
            session = await airtop_client.sessions.create(configuration=config)
            session_id = session.data.id
            
            # Create window
            window = await airtop_client.windows.create(session_id, url="https://www.google.com")
            window_id = window.data.window_id
            
            # Wait for page load
            await asyncio.sleep(2)
            
            # Test page query
            result = await airtop_client.windows.page_query(
                session_id=session_id,
                window_id=window_id,
                prompt="What is the title of this page?",
                configuration=PageQueryConfig()
            )
            
            # Terminate session
            await airtop_client.sessions.terminate(session_id)
            
            return {
                "airtop_type": str(type(airtop_client)),
                "available_methods": methods,
                "test_result": str(result),
                "test_success": True
            }
        except Exception as e:
            return {
                "airtop_type": str(type(airtop_client)),
                "available_methods": methods,
                "test_error": str(e),
                "test_success": False
            }
            
    except Exception as e:
        return {"error": str(e)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
    