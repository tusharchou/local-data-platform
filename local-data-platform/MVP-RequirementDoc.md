
# MVP Product Requirement Doc
**No** |**Key Updates** | **Date Updated** | **Updated by**
-- | -- | -- | -- |
1 | First Draft Completed |  28 Nov 2024 | Arpita Dhir |  
2 | Team Review | 28 Nov 2024 | Tushar and Mrtunjay |  

## Objective

This outlines the requirements for Local Data Platform, a web-based solution aimed at helping businesses make data-driven decisions. 

The platform will allow users to:

1. Search for User insights
2. Predict User Behaviour wrt Business
3. Upload Business data for analysis
4. Download User Insights as CSV files

### **Problem Statement**

Businesses often struggle to make informed decisions due to a lack of actionable insights. Access to predictive analytics tailored to their specific datasets remains limited, expensive, or overly complex.

### **Who are we solving for?**

- **Small to Medium-Sized Enterprises (SMEs)**: Businesses with limited access to dedicated data science teams or tools.
- **Decision-Makers**: Business owners, managers, or analysts seeking clear, actionable insights from their data.

### **Reasons it may not work?**
1. Users might hesitate to upload sensitive business data
2. Insights might be too complex or irrelevant for the target audience
3. Issues with data ingestion, prediction accuracy, or CSV export functionality
4. Low adoption due to insufficient marketing


## **Phases**

### MVP Mission
1. Allow users to Log in/Sign up
2. Users should have business insights dashboard
3. They can search insights 
4. They can manage insights i.e add and delete
6. Allows users to upload data to LDP 
7. Enables downloading of actionable insights in CSV format.

## **Success Metrics**

**Adoption Metrics**
- Number of registered businesses [Aim: 10]
- Percentage of users actively uploading data [Aim: 75%]
- Cost to conversion [Aim: 2$]

**Engagement Metrics**
- Time spent on the platform [Aim: 2 hr]
- Number of insights searched per user per hour [Aim: 5+]
- Number of insights downloaded per user per hour [Aim: 5+]

**Customer Satisfaction**
- User retention rates [Aim: 50%]
- User Churn rates [Aim: 20%]
- NPS Feedback [70]


Phase | Task | Timeline | Owner | Deliverables
-- | -- | -- | -- | --
Planning | Finalize PRD | 24 Nov 24 | Tushar | Approved Doc
Design | Create the handoff ready website design  | 20 Dec 24 | Arpita | High-fidelity designs
Frontend Dev | Build website on Framer | 24 Dec 24 | Arpita | Local Website
Backend Dev | API and package integration | 24 Dec 24 | Tushar | APIs and backend services
Integration | Combine frontend and backend | 28 Dec 24 | Tushar | Fully functional Staging Website 
Testing | QA and bug fixing | 28 Dec 24 | Tushar | Bug-free release-ready product
Marketing | Social posts to engage till Launch | 15 Dec 24 - 15 Jan 25 | Arpita | Social Posts
Deployment | Launch MVP | 15 Jan 25 | Tushar | Live platform

## Specifications 
### **Product Design**
- **Key Actions**
    - Authentication & Onboarding
    - Upload local data/ Ingest data
    - Search for insights
    - Download button for CSV exports.
    - Landing page with USP
    - Dashboard page 
        - showcasing insights
        - data upload/download options
        - pinned data insights
    - **Frontend**: Framer
    - Responsive Design

### **Backend Requirements**
- **Framework**: Python
- **Key Actions**:
    - Data ingestion APIs for parsing and validating uploaded files.
    - Prediction model powered by LDP
    - RESTful APIs for frontend-backend communication.
    - Export functionality to create CSV files for download.
- **Database**: 

### **Testing**
- **Unit Testing**: Validate frontend components and backend APIs.
- **Integration Testing**: Ensure seamless interaction between frontend and backend.
- **User Acceptance Testing (UAT)**: Validate product functionality with beta users.
