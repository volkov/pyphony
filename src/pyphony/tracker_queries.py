"""GraphQL query strings for the Linear issue tracker API."""

from __future__ import annotations

CANDIDATE_ISSUES_QUERY = """
query CandidateIssues($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      relations(first: 100) {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ISSUE_STATES_BY_IDS_QUERY = """
query IssueStatesByIds($ids: [ID!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      id: { in: $ids }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      state { name }
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""

ISSUE_UPDATE_STATE_MUTATION = """
mutation IssueUpdateState($issueId: String!, $stateId: String!) {
  issueUpdate(id: $issueId, input: { stateId: $stateId }) {
    success
    issue { id state { name } }
  }
}
"""

ISSUE_TEAM_QUERY = """
query IssueTeam($issueId: String!) {
  issue(id: $issueId) {
    team { id }
  }
}
"""

WORKFLOW_STATES_QUERY = """
query WorkflowStates($teamId: ID!) {
  workflowStates(filter: { team: { id: { eq: $teamId } } }) {
    nodes { id name }
  }
}
"""

COMMENT_CREATE_MUTATION = """
mutation CommentCreate($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) {
    success
    comment { id body }
  }
}
"""

ISSUES_BY_STATES_QUERY = """
query IssuesByStates($projectSlug: String!, $stateNames: [String!]!, $first: Int!, $after: String) {
  issues(
    filter: {
      project: { slugId: { eq: $projectSlug } }
      state: { name: { in: $stateNames } }
    }
    first: $first
    after: $after
  ) {
    nodes {
      id
      identifier
      title
      description
      priority
      state { name }
      branchName
      url
      labels { nodes { name } }
      relations(first: 100) {
        nodes {
          type
          relatedIssue {
            id
            identifier
            state { name }
          }
        }
      }
      createdAt
      updatedAt
    }
    pageInfo {
      hasNextPage
      endCursor
    }
  }
}
"""
